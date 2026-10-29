# Restricted backend deployment

GitHub Actions connects as `durov` using a separate backend-only key. The key is
restricted to `/usr/local/sbin/durov-gh-backend`; shell, SCP/SFTP, PTY and forwarding
are unavailable. The dispatcher validates exactly `deploy <40 lowercase hex SHA>`
and invokes `/usr/local/sbin/soborum-backend-deploy` through one no-arguments sudo
rule. Both installed files and their parent paths are root-owned and not writable
by deployment users. They are reviewed and installed separately, never executed
directly from this repository or automatically replaced by a release.

The helper uses a protected bare repository at `/var/lib/soborum-backend-ci`, a
fixed HTTPS upstream and `main`, successful GitHub Actions checks for the exact
SHA, an exclusive lock and fast-forward ancestry. It does not load the legacy
checkout's Git configuration, hooks or executable files. A pure content check
rejects changed legacy tracked files and unexpected untracked files. Original
`.env`, `.env.mcp` and Compose override files are preserved, pinned by hashes and
copied into protected configuration storage. Configuration changes require owner
review; CI does not upload, clear or rewrite application secrets.

Application releases are built from root-controlled Git objects in protected
release directories, without private configuration in the image build context.
The original checkout is retained. The Compose project and persistent database
and storage volume names stay unchanged. Only the backend service is rebuilt;
the database service is not recreated. Workflow/helper/test/doc-only changes
verify readiness and record the requested revision without restarting the app.
Results report both the verified main revision and active application revision.

Before application changes, the helper saves and validates code, private config,
a fresh PostgreSQL custom-format dump, storage and the previous Docker image.
Archive validation is not a restore rehearsal. Migration changes stop deployment
for separate review. Failed readiness restores the previous application image
and source with the same configuration; it never restores or downgrades the live
database. Backups and state are private to root. Automatic retention/deletion is
not implemented.

The existing `AllowUsers durov`, owner access and retired `deploy` keys are not
changed. `SSH_KNOWN_HOSTS` contains the previously verified host public key;
Actions requires strict checking. Repository secrets used by deployment are
`SSH_PRIVATE_KEY`, `SSH_USER`, `SSH_HOST` and `SSH_KNOWN_HOSTS`.

Local security checks (no Docker/database changes):

```sh
python -m unittest discover -s tests -p test_backend_ci.py -v
```
