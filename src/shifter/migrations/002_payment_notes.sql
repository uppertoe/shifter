-- Free-form per-nanny payment template (BSB+account, PayID, etc.).
-- Each non-empty line is shown in the Mark Paid flow with a one-tap "Copy" button.
ALTER TABLE nannies ADD COLUMN payment_notes TEXT;
