# Repository guidance

- Keep tracked content reusable for the public project. Store personal notes, chat handoffs, deployment records, app compatibility captures and installation-specific evidence under ignored `.local/private-docs/` or outside the checkout.
- Never force-add local state, credentials, runtime pointers, native project/controller inputs or private test artifacts. `.local/` is excluded from both Git and release packaging; other ignored paths are not automatically excluded from packages.
- Keep the root README current and follow its Standard Readme structure. Put repeatable validation instructions in `VERIFICATION.md`; keep individual run logs local.
- Preserve app-owned authorization, revisions and ambiguous-outcome handling. Use isolated synthetic fixtures for routine checks. Do not connect to native projects/devices or migrate registrations without task authorization.
- Follow `VERIFICATION.md` for checks appropriate to the change. Regenerate the source manifest after distributed-file changes and verify Git/exported bytes as well as the working tree.
- Commit coherent verified increments. Do not push unless the user explicitly authorizes it.
