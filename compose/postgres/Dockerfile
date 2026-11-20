# CI's one home of the Postgres base-image pin: Dependabot's docker
# entry (.github/dependabot.yml) updates this FROM line. The image step
# of .github/actions/postgres-image/action.yml derives its pull ref, ECR fallback,
# local tag and cache key from it; compose.yaml builds this directory
# locally, and CI's smoke override feeds the action's tag output.
FROM mirror.gcr.io/library/postgres:16.16@sha256:3950ad7c3148ca2175ef1b6c36c9efbf78957ae8fbe8b3a5d7c35af50500382f
