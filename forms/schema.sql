-- cronus-forms D1 database (binding: DB). Safe to run more than once.

-- One row per paid checkout. session_id is the provisioning idempotency key.
create table if not exists clients (
  session_id            text primary key,
  customer_id           text,
  subscription_id       text unique,
  email                 text not null,
  name                  text,
  plan                  text,
  amount_total          integer,
  currency              text,
  status                text not null default 'active',   -- active | cancelled
  created_at            integer not null,                 -- ms since epoch
  notified_at           integer,                          -- "new client" email sent
  endpoints_received_at integer,                          -- endpoint list form came in (matched by email)
  nudged_at             integer,                          -- "no endpoint list yet" reminder sent
  cancelled_at          integer,
  cancel_notified_at    integer
);
create index if not exists clients_email_idx on clients (lower(email));
create index if not exists clients_status_idx on clients (status);

-- Stripe event ledger. A row is claimed before processing and marked done after;
-- on failure it is deleted so Stripe's retry can claim it again.
create table if not exists stripe_events (
  event_id      text primary key,
  type          text not null,
  status        text not null default 'processing',      -- processing | done
  attempts      integer not null default 1,
  received_at   integer not null,
  processed_at  integer
);

-- Heartbeats: one row per monitored automation (Zap, Make scenario, cron job).
-- Register one:  insert into heartbeats (id, label, created_at)
--                values ('hb_<24 hex>', '<client> / <automation>', <ms since epoch>);
-- (python sentinel.py new-heartbeat prints a fresh id and the exact statement.)
create table if not exists heartbeats (
  id          text primary key,           -- hb_ + 24 hex characters
  label       text,                       -- for the operator only; never shown publicly
  created_at  integer not null,           -- ms since epoch
  last_ping   integer,                    -- ms since epoch; null until the first check-in
  pings       integer not null default 0
);

-- Telegram groups that added @cronus_alerts_bot (written by the /api/telegram/webhook handler).
create table if not exists telegram_chats (
  chat_id     text primary key,
  title       text,
  type        text,                       -- group | supergroup
  status      text not null,              -- active | removed | migrated
  added_by    text,
  first_seen  integer not null,           -- ms since epoch
  updated_at  integer not null
);

-- Monitored endpoints. Forms save them as 'pending'; the operator activates a client by
-- clicking "Send alerts here" in a Telegram/Slack connected email (or the endpoint list email).
-- sentinel.py reads the active rows from GET /api/sentinel/endpoints every run.
create table if not exists endpoints (
  client_id          text not null,            -- agency-slug--client-slug
  endpoint_name      text not null,
  target_url         text not null,
  config             text not null default '{}', -- other sentinel.py fields as JSON (expected_status, method, ...)
  agency             text,
  contact_email      text,
  source             text not null,            -- pilot | pro | manual
  telegram_chat_id   text,
  slack_webhook_url  text,
  status             text not null default 'pending',  -- pending | active | paused
  created_at         integer not null,         -- ms since epoch
  updated_at         integer not null,
  activated_at       integer,
  primary key (client_id, endpoint_name)
);
create index if not exists endpoints_status_idx on endpoints (status);
create index if not exists endpoints_email_idx on endpoints (lower(contact_email));

-- Slack channels connected through "Add to Slack". The URL is a credential: never log it.
create table if not exists slack_hooks (
  id          integer primary key autoincrement,
  team        text,
  channel     text,
  url         text not null unique,
  created_at  integer not null
);
