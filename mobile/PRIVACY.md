# Jaro mobile privacy

Jaro uses microphone access to turn speech into text on your device. The keyboard returns that text to the active app. That app handles the inserted text under its own privacy policy.

On iOS, Full Access lets Jaro's keyboard and containing app share settings, models, and dictation results through Jaro's App Group. Jaro requests microphone and Apple speech recognition access as needed, and requests on-device recognition. Warm-session settings can keep microphone access available for keyboard dictation until the session ends. Jaro provides separate controls for local typing personalization and optional local typing logs. Typing logs expire after 14 days and can be deleted from the app's privacy settings.

On Android, the keyboard processes audio locally. The starter model is included in the app. Optional downloads retrieve speech model files and expose ordinary connection information, such as your IP address, to the download host. iOS may also need network access to download model assets.

The sideloaded Android Preview checks Jaro’s release manifest on GitHub by default. You can turn off automatic checks on Home. GitHub receives routine connection information such as your IP address. The request contains no audio, typing, transcripts, or vocabulary. Choosing to download opens your browser.

The mobile apps have no Jaro account, analytics upload, or automatic feedback upload. Opening a support or privacy page connects to the site you open. You can clear on-device data through the available in-app controls or remove the app and keyboard through system settings.

[Contact Jaro](https://www.jard.ai/jaro) · [Report a security issue](../SECURITY.md)
