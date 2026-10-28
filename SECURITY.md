# Security

## Reporting a problem

Please don't open a public issue for a security problem. Report it privately instead:
**Security ▸ Report a vulnerability** on this repository's GitHub page. You'll get a reply within a few days.

Things worth reporting include anything that could:

- get an app update installed that wasn't released by StitchPilot
- run code or read files on someone's computer through a picture, embroidery file or project file
- use StitchPilot Plus AI features without a subscription, or reach someone else's account
- expose keys or other secrets

## How StitchPilot is kept safe

- **App updates are signed.** The app only installs an update signed with the StitchPilot release key, checks
  the download against what was signed, and never goes back to an older version. The key is kept offline,
  never on GitHub or the update server. See [deploy/aws/UPDATES.md](deploy/aws/UPDATES.md).
- **Builds are traceable.** Release files are built by GitHub Actions from this repository's code. Each one
  carries a build attestation that you can check:
  `gh attestation verify <file> -R jessiepullaro414/StitchPilot`
- **No secrets in the app or the repository.** AI keys live only on the StitchPilot Plus server, which checks
  each person's subscription itself. The app holds nothing that would let anyone get around that.
- **Pinned packages.** Python packages and GitHub actions are pinned to exact versions. Updates come through
  Dependabot pull requests, and the tests run on every one.
