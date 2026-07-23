-- Slayer / countable achievement steps: required kill (or complete) targets
-- from client AchievementAssociationsClient.txt (code^count).
-- Additive only — safe to re-run.

alter table public.achievement_steps
  add column if not exists association_code integer,
  add column if not exists target_count integer,
  add column if not exists target_kind text,
  add column if not exists target_mobs text;

comment on column public.achievement_steps.association_code is
  'Client component association code (joins AchievementAssociationsClient count).';
comment on column public.achievement_steps.target_count is
  'Required count for this step (e.g. 100 froglok kills). Null = not a counted step.';
comment on column public.achievement_steps.target_kind is
  'kill | complete_child | other — how the journal should progress this step.';
comment on column public.achievement_steps.target_mobs is
  'Human-readable target list from client step text (e.g. Frogloks and Tadpoles).';

create index if not exists achievement_steps_target_count_idx
  on public.achievement_steps (achievement_id)
  where target_count is not null;
