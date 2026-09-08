// Set by the generated instance entry point before importing the application.
let profile = 'full';
function configureInstallation(config) {
  profile = config.profile === 'imessage' ? 'imessage' : 'full';
}
function nativeOnly() { return profile === 'imessage'; }
export { configureInstallation, nativeOnly };
