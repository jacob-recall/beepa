// Address selection only; this module has no requests, credentials or DOM work.
export function masterTransport(location, config = {}) {
  const legacyLocal = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)
    && location.port === '8011';
  const serverName = typeof config.serverName === 'string'
    && /^[A-Za-z0-9.-]+(?::[0-9]+)?$/.test(config.serverName) ? config.serverName : 'master';
  return {
    csBase: legacyLocal ? 'http://127.0.0.1:8018' : location.origin,
    enrollBase: legacyLocal ? 'http://127.0.0.1:8019' : location.origin,
    serverName,
    allowLocalBootstrap: legacyLocal,
  };
}
