# Contributing

Use [GitHub issues](https://github.com/votecoffee/CodexMCPBroker/issues) for questions, bugs and proposed changes. Pull requests should describe the behavior being changed and the checks performed.

## Development

Follow [VERIFICATION.md](VERIFICATION.md) to create an environment, install the development lock, run tests and check formatting. Keep changes focused and preserve app-owned authorization, revision guards and uncertain-outcome handling. Use synthetic app fixtures for automated tests; native app/device workflows require a separately scoped validation plan.

## Repository contents

Public files should help any user install, integrate, develop or troubleshoot the broker. Use generic paths, synthetic examples and reusable instructions.

Keep these under ignored `.local/private-docs/` or outside the checkout:

- Personal notes, chat handoffs, deployment history and machine-specific setup records.
- App-specific compatibility captures, timing logs and validation reports from a particular installation.
- Customer/project/controller identifiers, source inputs and native engineering artifacts.
- Credentials, environment snapshots, generated runtime pointers and local service registrations.

`.local/` is excluded from Git and source packaging. Do not force-add its files. Review staged changes and exported archives for private data; a Git ignore rule does not remove content that was already committed. Generalize useful findings into product documentation or synthetic tests before sharing them.

## Documentation and releases

Keep the root README current with capabilities, installation, usage and limitations. Its structure follows [Standard Readme](https://github.com/RichardLitt/standard-readme). Keep the MIT license and attribution intact.

Regenerate `manifest.json` with `scripts/package_release.py` after changing distributed files. Verify the inventory from both a fresh Git export and extracted source ZIP so line endings and private-file exclusions are covered. Store local run evidence in the ignored directory. Commit coherent, verified changes; publish only with the repository owner's authorization.
