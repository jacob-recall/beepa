#!/usr/bin/env python3
"""Synthetic evidence only: never contacts.app, chat.db or installed bridges."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agents.enrich.crm_resolver import ContactRoomReader, provider_query


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.contacts = root / 'contacts.sqlite'
        with sqlite3.connect(self.contacts) as db:
            db.execute('CREATE TABLE contacts(source TEXT, network_id TEXT, version INTEGER, deleted INTEGER)')
            db.execute("INSERT INTO contacts VALUES('imessage','person@example.test',7,0)")
        self.native = root / 'native.sqlite'
        with sqlite3.connect(self.native) as db:
            db.execute('CREATE TABLE map(chat_id TEXT, room_id TEXT)')
            db.execute("INSERT INTO map VALUES('any;-;person@example.test','!dm:local')")
            db.execute('CREATE TABLE meta(k TEXT,v TEXT)')
            db.execute("INSERT INTO meta VALUES('mgmt_room','!management:local')")
        config = root / 'daemon.json'
        config.write_text(json.dumps({'user_id':'@user:local','bot_id':'@native:local','domain':'local'}))
        self.owner, self.generation = '@owner:master', 'generation1'
        self.contact = {'source':'imessage','network_id':'person@example.test'}
        self.policy, self.overrides, self.profiles = {'global':'share-all'}, {}, []
        def event(kind, content, key='', sender='@native:local'):
            return {'type':kind,'state_key':key,'content':content,'sender':sender,'event_id':'$' + kind}
        self.event = event
        common = [event('m.room.create', {}), event('m.room.member', {'membership':'join'}, '@user:local'), event('m.room.member', {'membership':'join'}, '@native:local')]
        self.states = {
            '!management:local': common + [event('com.jkali.bridge.mgmt', {'bridge':'imessage','role':'management'}, 'imessage'), event('com.jkali.imessage_health', {'poll_ok':True,'updated_at':1000})],
            '!dm:local': copy.deepcopy(common) + [event('uk.half-shot.bridge', {'bridgebot':'@native:local','protocol':{'id':'imessage'},'channel':{'id':'any;-;person@example.test'}},'local/imessage')],
        }
        self.reader = ContactRoomReader(contacts_db=self.contacts, local_user_mxid='@user:local',
            read_context=lambda:{'owner_mxid':self.owner,'generation':self.generation},
            read_room_state=lambda room:copy.deepcopy(self.states[room]),
            read_profiles=lambda:copy.deepcopy(self.profiles), read_contact_policy=lambda:self.policy,
            read_contact_overrides=lambda:self.overrides, imessage_db=self.native,
            imessage_config=config, clock=lambda:1000)

    def resolve(self, **kwargs):
        args = dict(contact=self.contact, contact_version='7', owner_mxid='@owner:master', generation='generation1')
        args.update(kwargs)
        return self.reader.resolve_contact(**args)

    def test_private_conversation_is_resolved_without_mirror_or_message_access(self):
        self.states['!dm:local'].append(self.event('com.jkali.share_override', {'state':'private'}))
        result = self.resolve()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual([r['room_id'] for r in result['rooms']], ['!dm:local'])
        self.assertEqual(result['rooms'][0]['bridge_epoch_kind'], 'local_bridge_incarnation')
        self.assertEqual(result, self.resolve())

    def test_shared_version_and_pairing_required(self):
        for args in ({'contact_version':7},{'contact_version':'6'},{'owner_mxid':'@foreign:master'},{'generation':'old'}):
            with self.subTest(args=args):
                self.assertEqual(self.resolve(**args)['status'], 'stale')
        self.overrides['imessage|person@example.test'] = 'private'
        self.assertEqual(self.resolve()['status'], 'unshared')
        self.overrides = None
        self.assertEqual(self.resolve()['status'], 'unavailable')

    def test_deleted_missing_and_malformed_versions_never_resolve(self):
        for version, deleted, expected in ((8,0,'stale'), (7,1,'unshared'), (None,0,'unavailable'), (0,0,'unavailable')):
            with sqlite3.connect(self.contacts) as db:
                db.execute('UPDATE contacts SET version=?,deleted=?', (version,deleted))
            self.assertEqual(self.resolve()['status'], expected)

    def test_missing_native_database_does_not_create_file(self):
        self.reader.imessage_db = Path(self.temp.name) / 'missing.sqlite'
        self.assertEqual(self.resolve()['status'], 'unavailable')
        self.assertFalse(self.reader.imessage_db.exists())

    def test_health_requires_fresh_success_from_exact_bot(self):
        health = self.states['!management:local'][-1]
        original = copy.deepcopy(health)
        for content in ({'poll_ok':False,'updated_at':1000},{'updated_at':1000},{'poll_ok':True,'updated_at':399},{'poll_ok':True,'updated_at':1031},{'poll_ok':True,'updated_at':True},{'poll_ok':True,'updated_at':float('nan')}):
            health['content'] = content
            self.assertEqual(self.resolve()['status'], 'unavailable')
        health.update(original)
        health['sender'] = '@forged:local'
        self.assertEqual(self.resolve()['status'], 'stale')

    def test_malformed_or_spoofed_portal_identity_fails(self):
        original = copy.deepcopy(self.states['!dm:local'])
        for field, value in (('bridgebot','@foreign:local'),('protocol',{'id':'whatsapp'}),('channel',{'id':'any;-;other@example.test'}),('protocol',None)):
            self.states['!dm:local'] = copy.deepcopy(original)
            self.states['!dm:local'][-1]['content'][field] = value
            self.assertNotEqual(self.resolve()['status'], 'ready')
        self.states['!dm:local'] = original
        self.states['!dm:local'][0]['sender'] = '@foreign:local'
        self.assertEqual(self.resolve()['status'], 'stale')

    def test_management_space_tombstone_and_left_member_fail(self):
        original = copy.deepcopy(self.states['!dm:local'])
        for event in (self.event('m.room.tombstone',{}),self.event('com.jkali.bridge.mgmt',{},'anything')):
            self.states['!dm:local'] = copy.deepcopy(original) + [event]
            self.assertEqual(self.resolve()['status'], 'stale')
        self.states['!dm:local'] = copy.deepcopy(original)
        self.states['!dm:local'][0]['content']['type'] = 'm.space'
        self.assertEqual(self.resolve()['status'], 'stale')
        self.states['!dm:local'] = original
        self.states['!dm:local'][1]['content']['membership'] = 'leave'
        self.assertEqual(self.resolve()['status'], 'stale')

    def test_groups_are_not_counterpart_matches(self):
        with sqlite3.connect(self.native) as db:
            db.execute("UPDATE map SET chat_id='any;+;person@example.test'")
        self.assertEqual(self.resolve()['status'], 'no_existing_conversation')

    def test_replaced_connection_rooms_cannot_attest_current_incarnation(self):
        self.states['!management:local'].append(self.event('m.room.tombstone',{'replacement_room':'!new:local'}))
        self.assertEqual(self.resolve()['status'],'stale')
        contact = self.provider()
        self.states['!login:local'].append(self.event('m.room.tombstone',{'replacement_room':'!new:local'}))
        self.assertEqual(self.resolve(contact=contact)['status'],'stale')

    def test_duplicate_contact_mapping_is_ambiguous(self):
        with sqlite3.connect(self.native) as db:
            db.execute("INSERT INTO map VALUES('any;-;PERSON@example.test','!other:local')")
        self.states['!other:local'] = copy.deepcopy(self.states['!dm:local'])
        self.states['!other:local'][-1]['content']['channel']['id'] = 'any;-;PERSON@example.test'
        self.assertEqual(self.resolve()['status'], 'ambiguous')

    def test_partial_profile_scope_cannot_grant_only_easy_room(self):
        self.profiles = [{'id':'p','handleIds':[self.contact],'roomIds':['!dm:local','!unknown:local']}]
        self.assertEqual(self.resolve()['status'], 'unavailable')
        self.profiles.append(copy.deepcopy(self.profiles[0]))
        self.assertEqual(self.resolve()['status'], 'ambiguous')

    def test_race_and_exception_details_fail_closed(self):
        calls = []
        def context():
            calls.append(1)
            return {'owner_mxid':self.owner,'generation':'generation1' if len(calls)==1 else 'generation2'}
        self.reader.read_context = context
        self.assertEqual(self.resolve()['status'], 'stale')
        self.reader.read_context = lambda: (_ for _ in ()).throw(ValueError('SECRET person@example.test'))
        result = self.resolve()
        self.assertEqual(result['rooms'], [])
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('person@example.test', json.dumps(result))

    def provider(self, source='gmessages', handle='+14155552671'):
        bot, space, room = '@' + source + 'bot:local', '!login:local', '!provider:local'
        protocol = 'instagramgo' if source=='instagram' else source
        self.provider_status = {'network':{'network_id':source,'beeper_bridge_type':protocol},
            'bridge_bot':bot,'logins':[{'id':'login1','space_room':space,'state':{'state_event':'CONNECTED'}}]}
        self.provider_rows = [{'room_id':room,'native_id':'thread1','receiver':'login1','room_type':'dm',
            'other_user_id':'12345','owner':'@user:local','login_id':'login1','login_space':space,
            'bridge_id':'','phone':handle,'identifiers':[]}]
        self.reader.read_provisioning = lambda _:copy.deepcopy(self.provider_status)
        self.reader.query_provider = lambda _:copy.deepcopy(self.provider_rows)
        event = lambda kind, content, key='':self.event(kind,content,key,bot)
        self.states[space] = [event('m.room.create',{'type':'m.space'}),event('m.room.member',{'membership':'join'},'@user:local'),event('m.room.member',{'membership':'join'},bot)]
        self.states[room] = [event('m.room.create',{}),event('m.room.member',{'membership':'join'},'@user:local'),event('m.room.member',{'membership':'join'},bot),
            event('uk.half-shot.bridge', {'bridgebot':bot,'creator':bot,'protocol':{'id':protocol},'channel':{'id':'thread1','fi.mau.receiver':'login1'},'com.beeper.room_type.v2':'dm'},'local/'+source)]
        contact = {'source':source,'network_id':handle}
        with sqlite3.connect(self.contacts) as db:
            db.execute('INSERT INTO contacts VALUES(?,?,7,0)',(source,handle))
        return contact

    def test_provider_current_login_ownership_and_portal_proof(self):
        contact = self.provider()
        self.assertEqual(self.resolve(contact=contact)['status'],'ready')
        original = copy.deepcopy(self.provider_rows[0])
        for key, value, status in (('owner','@foreign:local','no_existing_conversation'),('login_id','dead','stale'),('receiver','foreign','stale'),('bridge_id','old-bridge','stale'),('room_type','group_dm','stale'),('login_space','!old:local','stale')):
            with self.subTest(key=key):
                self.provider_rows[0] = dict(original, **{key:value})
                self.assertEqual(self.resolve(contact=contact)['status'],status)
        self.provider_rows[0] = original
        self.provider_status['logins'][0]['state']['state_event'] = 'BAD_CREDENTIALS'
        self.assertEqual(self.resolve(contact=contact)['status'],'unavailable')

    def test_provider_group_dm_and_mismatched_matrix_channel_fail(self):
        contact = self.provider()
        content = self.states['!provider:local'][-1]['content']
        content['com.beeper.room_type.v2'] = 'group_dm'
        self.assertEqual(self.resolve(contact=contact)['status'],'stale')
        content['com.beeper.room_type.v2'] = 'dm'
        content['channel']['fi.mau.receiver'] = 'other'
        self.assertEqual(self.resolve(contact=contact)['status'],'stale')
        content['channel']['fi.mau.receiver'] = 'login1'
        content['channel']['id'] = 'other-thread'
        self.assertEqual(self.resolve(contact=contact)['status'],'stale')

    def test_linked_profile_resolves_private_cross_source_room(self):
        self.provider()
        self.profiles = [{'id':'p','handleIds':[self.contact],'roomIds':['!dm:local','!provider:local']}]
        result = self.resolve()
        self.assertEqual(result['status'],'ready')
        self.assertEqual([p['source'] for p in result['rooms']],['imessage','gmessages'])
        self.provider_rows[0]['owner'] = '@other:local'
        self.assertEqual(self.resolve()['status'],'stale')

    def test_all_v2_counterpart_id_shapes(self):
        for source, handle in (('whatsapp','+14155552671'),('gmessages','+14155552671'),('linkedin','member-stable-urn'),('instagram','12345'),('twitter','12345')):
            with self.subTest(source=source):
                contact = self.provider(source,handle)
                row = self.provider_rows[0]
                row['other_user_id'] = 'lid-123' if source=='whatsapp' else 'member-stable-urn' if source=='linkedin' else '12345'
                if source=='whatsapp': row['phone'] = '14155552671'
                self.assertEqual(self.resolve(contact=contact)['status'],'ready')

    def test_duplicate_login_binding_and_mid_read_account_change(self):
        contact = self.provider()
        self.provider_status['logins'].append(copy.deepcopy(self.provider_status['logins'][0]))
        self.assertEqual(self.resolve(contact=contact)['status'],'ambiguous')
        self.provider_status['logins'].pop()
        count = []
        def changing():
            count.append(1)
            rows = copy.deepcopy(self.provider_rows)
            if len(count)>1: rows[0]['login_id']='changed'
            return rows
        self.reader.query_provider = lambda _:changing()
        self.assertEqual(self.resolve(contact=contact)['status'],'stale')

    def test_fixed_sql_has_exact_owner_and_receiver_joins_no_credentials(self):
        for source in ('whatsapp','gmessages','instagram','linkedin','twitter'):
            query = provider_query(source)
            for required in ('p.bridge_id=up.bridge_id','p.receiver=up.portal_receiver','ul.user_mxid=up.user_mxid','g.bridge_id=p.bridge_id',"p.room_type='dm'"):
                self.assertIn(required,query)
            self.assertNotIn('ul.metadata',query)
        discord = provider_query('discord')
        self.assertIn('u.dcid=p.receiver',discord)
        self.assertIn('p.type=1',discord)
        self.assertIn('p.dc_guild_id IS NULL',discord)
        self.assertNotIn('discord_token',discord)

    def test_provisioning_uses_only_stdin_secret_and_fixed_read_route(self):
        root = Path(self.temp.name)
        (root/'gmessages').mkdir()
        (root/'gmessages/config.yaml').write_text('provisioning:\n    shared_secret: "synthetic_secret_1234567890"\n')
        self.reader.root = root
        payload = b'{"logins":[]}'
        calls = []
        def run(args, **kwargs):
            calls.append((args,kwargs))
            return SimpleNamespace(returncode=0,stdout=b'HTTP/1.0 200 OK\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
        self.reader.runner = run
        self.assertEqual(self.reader._provisioning('gmessages'),{'logins':[]})
        args, kwargs = calls[0]
        self.assertNotIn('synthetic_secret', ' '.join(args))
        self.assertIn(b'GET /_matrix/provision/v3/whoami?user_id=',kwargs['input'])
        self.assertNotIn('shell',kwargs)
        (root/'gmessages/config.yaml').write_text('shared_secret: disable\n')
        self.reader.read_provisioning = self.reader._provisioning
        contact = self.provider('linkedin','123')
        self.reader.read_provisioning = self.reader._provisioning
        self.assertEqual(self.resolve(contact=contact)['status'],'unavailable')

    def test_discord_legacy_dm_owned_by_live_receiver(self):
        contact = self.provider('discord','12345')
        root = Path(self.temp.name)
        (root/'discord').mkdir()
        (root/'discord/config.yaml').write_text('homeserver:\n    domain: local\nappservice:\n    bot:\n        username: discordbot\nbridge:\n    username_template: discord_{{.}}\n')
        self.reader.root = root
        self.provider_status = {'Discord':{'id':'789','connected':True,'logged_in':True}}
        row = self.provider_rows[0]
        row.update(login_id='789',receiver='789',room_type=1)
        state = self.states['!provider:local']
        ghost = '@discord_12345:local'
        state[0]['sender'] = ghost
        state[2]['state_key'] = ghost
        state[-1]['sender'] = ghost
        state[-1]['content'].update(creator=ghost,protocol={'id':'discordgo'})
        self.assertEqual(self.resolve(contact=contact)['status'],'ready')
        row['room_type']=3  # Discord group DM; legacy bridge info calls both dm.
        self.assertEqual(self.resolve(contact=contact)['status'],'stale')


if __name__ == '__main__':
    unittest.main()
