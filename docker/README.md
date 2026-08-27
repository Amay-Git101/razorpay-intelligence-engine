# docker

Per-service Dockerfiles (`apps/api`, `apps/web`) will live here once
those apps exist. The local infrastructure stack (Postgres, Redis) is
defined in the root `docker-compose.yml`.

Status: **Docker Desktop is not currently installed on the development
machine used for this repository.** `docker-compose.yml` at the repo
root has been written but has NOT been run or verified. Do not assume
the local stack works until it has actually been started successfully.
