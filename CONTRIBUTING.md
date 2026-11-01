# Contributing

Pull requests are welcome: bug fixes, tests, documentation, and features that fit the contract in [SPEC.md](SPEC.md). For a larger change, open an issue first so we can agree on the shape before you write the code.

## Development

You need [uv](https://docs.astral.sh/uv/); it brings Python and the dependencies. Run the whole suite from the repository root:

```bash
uv run --python 3.12 --with-requirements server/requirements.txt --with-requirements deploy/hosted/requirements.txt \
  --with httpx --with pytest python -m pytest server/tests -q
```

The tests need no network and no Google Cloud account (Google, Drive and Firestore are faked in memory). CI runs the same command on every pull request; it must pass before a merge.

## Guidelines

- Keep SPEC.md and the code in step: a change to fields, statuses, endpoints, tool signatures or the Drive file format updates SPEC.md in the same pull request. Files in users' Drives are read by other clients too, so changes to the file format must stay backward compatible.
- Add or update tests for every behaviour change.
- Never log tokens, secrets, authorization codes or users' data; never commit real credentials, chat ids, IP addresses or personal data (use `example.com` and obviously fake values in tests).
- Security problems go to [SECURITY.md](SECURITY.md), not to public issues.

By submitting a contribution you agree that it is licensed under the [Apache License 2.0](LICENSE), as stated in its section 5.
