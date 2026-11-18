-- ComputeWise schedules (B7): pg_cron rings, the FastAPI job endpoints do the work.
--
--   computewise-sync        every 5 minutes (D4)         POST /jobs/sync
--   computewise-rollover    1st of the month, 00:05 UTC  POST /jobs/rollover
--   computewise-agent-*     daily and weekly             POST /jobs/agent   (PENDING, see below)
--
-- All times are UTC, like the ledger's months (period_of(utcnow())).
-- The 5-minute sync also keeps the free Supabase project awake (D5).
--
-- ---------------------------------------------------------------------------------------
-- HOW TO RUN (once the backend is deployed, A9; until then there is nothing to call)
--
-- 1. Supabase dashboard > Database > Extensions: enable pg_cron and pg_net
--    (or let step 3 do it).
--
-- 2. Put the two secrets in Supabase Vault, ONCE, in the SQL editor. They are never
--    written in this file, because this file is on GitHub:
--
--      select vault.create_secret('https://<the deployed backend>', 'computewise_api_url');
--      select vault.create_secret('<the JOB_SECRET from the backend env>', 'computewise_job_secret');
--
--    computewise_api_url is the base the routes hang off, with no trailing slash: the
--    schedules call computewise_api_url || '/jobs/sync'. If the deployment serves the app
--    under a prefix (e.g. /api), include it here.
--    To change one later:
--      select vault.update_secret(id, '<new value>') from vault.secrets where name = 'computewise_job_secret';
--
-- 3. Run this whole file in the SQL editor. Safe to run again: it replaces the
--    ComputeWise jobs and touches no others.
--
-- 4. Check it (see the end of this file).
--
-- Tell the others before running it on the shared project (D18).
-- ---------------------------------------------------------------------------------------

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Remove earlier versions of OUR jobs only, so re-running never doubles them.
select cron.unschedule(jobid)
from cron.job
where jobname in ('computewise-sync', 'computewise-rollover',
                  'computewise-agent-daily', 'computewise-agent-weekly');

-- Every 5 minutes: usage -> spend, revocations, 80% / 100% rules, key limits.
-- pg_net gives up after 2 s by default; a sync can take longer, so allow 30 s.
select cron.schedule(
  'computewise-sync',
  '*/5 * * * *',
  $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_api_url') || '/jobs/sync',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'X-Job-Secret', (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_job_secret')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 30000
  );
  $$
);

-- 1st of the month at 00:05 UTC: leftovers carried over (D2), monthly budgets allocated.
-- Five minutes past midnight so the last sync of the old month has finished. The job is
-- safe to call twice, so a late or repeated run does no harm.
select cron.schedule(
  'computewise-rollover',
  '5 0 1 * *',
  $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_api_url') || '/jobs/rollover',
    headers := jsonb_build_object(
      'Content-Type', 'application/json',
      'X-Job-Secret', (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_job_secret')
    ),
    body := '{}'::jsonb,
    timeout_milliseconds := 30000
  );
  $$
);

-- PENDING (Person 3): the agent runs, daily at 05:00 UTC (07:00 Beirut) for warnings and
-- Mondays at 05:30 UTC for the weekly reports. POST /jobs/agent is not on main yet, and
-- what each run must send (daily vs weekly, one team per call?) is still to be agreed.
-- When it is, fill in the body and remove the comment marks.
--
-- select cron.schedule(
--   'computewise-agent-daily',
--   '0 5 * * *',
--   $$
--   select net.http_post(
--     url := (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_api_url') || '/jobs/agent',
--     headers := jsonb_build_object(
--       'Content-Type', 'application/json',
--       'X-Job-Secret', (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_job_secret')
--     ),
--     body := '{}'::jsonb,  -- TODO(Person 3): what the daily run sends
--     timeout_milliseconds := 30000
--   );
--   $$
-- );
--
-- select cron.schedule(
--   'computewise-agent-weekly',
--   '30 5 * * 1',
--   $$
--   select net.http_post(
--     url := (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_api_url') || '/jobs/agent',
--     headers := jsonb_build_object(
--       'Content-Type', 'application/json',
--       'X-Job-Secret', (select decrypted_secret from vault.decrypted_secrets where name = 'computewise_job_secret')
--     ),
--     body := '{}'::jsonb,  -- TODO(Person 3): what the weekly run sends
--     timeout_milliseconds := 30000
--   );
--   $$
-- );

-- ---------------------------------------------------------------------------------------
-- CHECKING IT
--
--   Our jobs:          select jobid, jobname, schedule, active from cron.job where jobname like 'computewise-%';
--   Did they run:      select jobid, status, return_message, start_time from cron.job_run_details
--                      order by start_time desc limit 10;
--   What the backend
--   answered (kept 6 h): select id, status_code, content, error_msg from net._http_response
--                      order by created desc limit 10;
--
--   A 401 means the job secret in Vault and JOB_SECRET in the backend differ.
--   {"skipped": true} on a sync means the previous one was still running (the lock).
--
--   Pause one:   select cron.alter_job(jobid, active := false) from cron.job where jobname = 'computewise-sync';
--   Remove all:  select cron.unschedule(jobid) from cron.job where jobname like 'computewise-%';
-- ---------------------------------------------------------------------------------------