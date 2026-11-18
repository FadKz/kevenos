# Shared staff roles during Rehearsal

Admin → People & access → People and roles → Edit access.
Keep Judge checked and its station selected. Select Organizer and Registration,
enter a reason, confirm that shared staff roles apply in Live and Rehearsal, then
Save roles. The person signs in again with the same password and Home offers all
assigned dashboards. This does not switch the event into Live mode.

Judge and Participant assignments remain separate for Live and Rehearsal. Shared
role edits require Admin access, a current account revision and an explicit
boolean acknowledgement when changed in Rehearsal. Guest reassignment, account
linking in Rehearsal, impersonated Admin writes and self-removal of Admin remain
blocked. A compact attributed shared-role audit record survives rehearsal resets;
existing intentional Live event reset semantics still clear the audit history.

Independent review: role_edit_review examined the diff against e1c11bd, found and
verified the durable audit correction, then reported no blocking findings.
20 focused Python tests and the Admin UI regression pass; browser fixture verified
adding Organizer to an existing Judge/Registration account with station retained.
No production account roles are changed as part of this software release.
