# CI's one home of the Postgres base-image pin: Dependabot's docker
# entry (.github/dependabot.yml) updates this FROM line. The image step
# of .github/actions/postgres-image/action.yml derives its pull ref, ECR fallback,
# local tag and cache key from it; compose.yaml builds this directory
# locally, and CI's smoke override feeds the action's tag output.
FROM mirror.gcr.io/library/postgres:16.15@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a
