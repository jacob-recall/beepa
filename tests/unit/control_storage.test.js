import assert from 'node:assert/strict';
import {mkdtempSync,chmodSync,statSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ControlCore} from '../../master/control-core.mjs';

const dir=mkdtempSync(join(tmpdir(),'beepa-control-path-')),cwd=process.cwd();
try{
  chmodSync(dir,0o755);process.chdir(dir);
  for(const path of ['-','control.db','nested/control.db','../control.db','/control.db',undefined]){
    assert.throws(()=>new ControlCore({path}),/absolute path inside a private state directory/);
    assert.equal(statSync(dir).mode&0o777,0o755,'invalid paths must not chmod the working directory');
    assert.deepEqual(readdirSync(dir),[],'invalid paths must not create files or directories');
  }
  const path=join(dir,'private','control.db'),core=new ControlCore({path});
  assert.equal(statSync(join(dir,'private')).mode&0o777,0o700);
  assert.equal(statSync(path).mode&0o777,0o600);core.db.close();
  assert.equal(statSync(dir).mode&0o777,0o755);
}finally{process.chdir(cwd);rmSync(dir,{recursive:true,force:true});}
console.log('Control storage: invalid paths leave parent permissions/files unchanged; explicit state paths stay private.');
