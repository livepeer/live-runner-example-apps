# Contributing to the live runner example apps

Thank you for considering contributing to the live runner example apps. These are teaching examples, so clarity beats cleverness — favor small, readable changes.

## Getting Started

Fork the repository, clone your fork, create a descriptive branch, commit your changes, push to your fork, and open a pull request. See [Making a Pull Request](https://github.com/susam/gitpr) if you're new to the flow.

### Issues

- **Found a bug or something unclear?** [Open an issue](https://github.com/livepeer/app-examples/issues) with what you expected, what happened, and the steps to reproduce.
- **Want a new transport or example?** Open an issue first to discuss scope before writing code.

### Commits

Follow the [Conventional Commits v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) pattern with a scope, matching the existing history: `feat(echo): ...`, `docs(vllm): ...`, `ci: ...`.

### Code Conventions

Formatting is enforced in CI ([black] for Python, [prettier] for Markdown/YAML/JSON) via [pre-commit]. To auto-format on commit instead of finding out on your PR, enable the hooks once per clone:

```sh
uvx pre-commit install
```

Or run them manually any time:

```sh
uvx pre-commit run --all-files
```

Local hooks are optional — CI runs the same checks either way.

### Pull Requests

- Keep PRs **small and focused** — one example or one concern per PR.
- Every example must run **offchain (free)** end to end; note in the PR if you tested on-chain too.
- Update the example's `README.md` and the root `README.md` table when behavior changes.

## Getting Help

Ask in the `#dev` channels of the [Livepeer Discord](https://discord.com/invite/livepeer).

[black]: https://black.readthedocs.io
[prettier]: https://prettier.io
[pre-commit]: https://pre-commit.com
