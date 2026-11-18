# CI's one home of the Postgres base-image pin: Dependabot's docker
# entry (.github/dependabot.yml) updates this FROM line. The image step
# of .github/actions/postgres-image/action.yml derives its pull ref, ECR fallback,
# local tag and cache key from it; compose.yaml builds this directory
# locally, and CI's smoke override feeds the action's tag output.
FROM mirror.gcr.io/library/postgres:16.16@sha256:dd2bf3ce2dc83a81674cf7f217fcfd8a5009a4b325dda5b843bfee82a06cff6f
