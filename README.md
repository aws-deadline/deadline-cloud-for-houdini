# Amazon Deadline Cloud for Houdini

This package has two active branches:

- `mainline` -- For active development. This branch is not intended to be consumed by other packages. Any commit to this branch may break APIs, dependencies, and so on, and thus break any consumer without notice.
- `release` -- The official release of the package intended for consumers. Any breaking releases will be accompanied with an increase to this package's interface version.

## Development

See [DEVELOPMENT](DEVELOPMENT.md) for more information.

## Build / Test / Release

### Build the package

```bash
hatch build
```

### Run tests

```bash
hatch run test
```

### Run linting

```bash
hatch run lint
```

### Run formatting

```bash
hatch run fmt
```

## Run tests for all supported Python versions

```bash
hatch run all:test
```

## Use development Submitter in Houdini

```bash
hatch run install
```

A development version of the Deadline Cloud node is then available in `/out` by pressing TAB, typing `deadline`, and adding it to the network.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This project is licensed under the Apache-2.0 License.
