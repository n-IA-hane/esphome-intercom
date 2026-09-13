# 2026.9.3-dev: start Assist calls directly in listening mode

This preview fixes the unwanted initial response when calling Home Assistant Assist. **2026.9.2 remains the stable release.**

The existing **Provide advanced call details to the assistant** option now controls the entire opening message:

- **Off:** Assist starts listening directly. No caller name, number or automatic text message is sent to the conversation agent.
- **On:** Assist receives the caller name and telephone details once at the beginning, then continues listening as before.

The option stays off by default. No new setting, custom greeting or change to other call routes is introduced.

## Install and test

1. Open VoIP Stack in HACS, enable prerelease versions if necessary, choose **Redownload**, and select **2026.9.3-dev**.
2. Restart Home Assistant.
3. In VoIP Stack's configuration, leave **Provide advanced call details to the assistant** off and call the Assist extension. Check that it waits for your speech without first replying to a caller-information message.
4. If you use the advanced details with a conversational agent, enable the option and check that your existing telephone behavior still works.

This is a Home Assistant integration update. ESP firmware does not need to be rebuilt.

Please report your conversation agent, the option state and the result in [issue #120](https://github.com/n-IA-hane/esphome-intercom/issues/120). The local automated checks pass; confirmation from the affected installation is welcome.
