# CI's one home of the Postgres base-image pin: Dependabot's docker
# entry (.github/dependabot.yml) updates this FROM line. The image step
# of .github/actions/postgres-image/action.yml derives its pull ref, ECR fallback,
# local tag and cache key from it; compose.yaml builds this directory
# locally, and CI's smoke override feeds the action's tag output.
FROM mirror.gcr.io/library/postgres:16.15@sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657
