-- Per-nanny "show on dashboard" toggle. Independent of `active`: an active
-- nanny might still be hidden from the dashboard (e.g. on leave) without
-- losing payment history or schedule access at /shifts and /nannies.
ALTER TABLE nannies
  ADD COLUMN show_on_dashboard INTEGER NOT NULL DEFAULT 1;
