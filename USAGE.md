# Starter Template Usage

## Quick start with generator

From this workspace, run:

```bash
./scripts/new_repo.sh /path/to/new-repo "Project Name"
```

If the project name is omitted, the destination folder name is used.

## 1) Create a new repository

Copy this template into a new project directory:

```bash
cp -R starter-template /path/to/new-repo
```

## 2) Customize immediately

- Set project name and setup instructions in `README.md`.
- Replace placeholder commands in `Makefile` for `test` and `build`.
- Add project-specific rules and architecture notes to `AGENTS.md`.
- Add `.env.example` with required variable names.

## 3) Validate baseline

```bash
make check
```

Then ensure:

```bash
make test
make build
```

## 4) Commit baseline

After customization, commit the initial skeleton so future work stays consistent.
