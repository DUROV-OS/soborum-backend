# Calendar records, attachments and native tasks

## Delivered in this change

- Add menu: meeting, native task, personal entry, anniversary. Wide responsive editor.
- Private files: PDF, DOC/DOCX, XLS/XLSX, JPG/JPEG, PNG, ZIP; 50 MiB each; at most 10 per record. Header/container validation, SHA-256, authenticated download, drafts recovery. Attachments belong to one event, are bound during its versioned save, and cannot be attached from someone else's draft. No claim of antivirus scanning.
- Soft deletion with optimistic version and request idempotency; event/change/file history retained. Removed files are no longer downloadable through current-event access.
- Organizer manages membership and roles; editors update details; coorganizers may delete. Only the organizer changes task assignment/review. Calendar remains available without business-module grants; actual task creation requires task edit access.
- A native task and its calendar event are saved in one transaction. Calendar owns title, description, deadline, assignee and reviewer; task board owns execution and acceptance. Linked task details cannot diverge via a second editor. Board responses reflect the native status. Event and task locks use the same order. Deleted tasks leave active boards and deadline alerts but retain report history.
- Tags, document links, per-user templates, existing availability suggestions, category filters and drag/drop. Updates from old clients preserve newly introduced fields. RSVP resets only when schedule/topic/location changes.

## Explicit boundaries

- A linked task is one dated task, not a repeated series. Repeated meetings remain supported. Task recurrence requires a separate occurrence-to-task lifecycle before enabling it.
- Whole-series editing/deletion remains the supported recurrence scope.
- Calendar event reminders currently run while Durov OS is open. Native task deadline notifications use the existing task subsystem. Calendar background push, delivery retry/outbox and external email invitations are not implemented by this change.
- Existing ICS export is a download, not bidirectional Google/Yandex synchronization. External synchronization requires per-user authorization and conflict/recurrence mapping; no external account is silently connected.
- Offline writes/synchronization, natural-language voice creation, operating-system widgets, auto-response and invitation-forwarding rules remain separate work. None is represented as functional in this release.
- No user-facing restore from the audit trail yet; deletion is a retained tombstone, not destruction of evidence.

## Database and release

`cal1711202601` follows `cal1611202601` and only adds `team_calendar_files`; event/task additions use existing JSON fields. Validate migration and operations on an isolated PostgreSQL clone before deployment, back up first, then publish the client. The reverse proxy must allow a 50 MiB multipart request including overhead. Existing production Caddy configuration has no explicit request-body cap.

## Verification

API regression tests cover maximum file size, foreign-file access/binding, atomic failure, audit-preserving deletion, stale versions, role permissions, old-client compatibility, task-board visibility/status/source-of-truth, and private templates. Browser checks use synthetic data only: create/upload/save, task-board navigation, template save, deletion without reload, and 390px mobile layout. Existing timezone/layout/export/access checks remain required.
