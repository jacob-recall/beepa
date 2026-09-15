// Set by the generated instance entry point before importing the application.
let profile = 'full';
let loginAlias = '';
let loginUser = '';
let helpers = {};
function configureInstallation(config) {
  profile = config.profile === 'imessage' ? 'imessage' : 'full';
  loginAlias = typeof config.loginAlias === 'string' ? config.loginAlias : '';
  loginUser = typeof config.loginUser === 'string' ? config.loginUser : '';
  helpers = config.helpers || {};
}
function nativeOnly() { return profile === 'imessage'; }
function resolveLoginName(user) {
  return loginAlias && loginUser && user.trim() === loginAlias ? loginUser : user;
}
function helperBase(name) {
  const base = helpers[name] || ({gmessages: 'http://127.0.0.1:8020', session: 'http://127.0.0.1:8021'})[name];
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base || '')) throw new Error('Invalid local helper configuration');
  return base;
}
export { configureInstallation, nativeOnly, resolveLoginName, helperBase };
