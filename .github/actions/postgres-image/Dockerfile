# CI's one home of the Postgres base-image pin: Dependabot's docker
# entry (.github/dependabot.yml) updates this FROM line. The image step
# of action.yml beside this file derives its pull ref, ECR fallback,
# local tag and cache key from it; compose.yaml builds this directory
# locally, and CI's smoke override feeds the action's tag output.
FROM mirror.gcr.io/library/postgres:16.15@sha256:9e2cf6287f25c3151f4c9421ccce2b17f3e60061c111a29165e0d2cdd9ff0a97
