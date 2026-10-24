# Rollout plan v3

Release 4.3 (new billing dashboard). Approved on 20 October by Alice (product) and Carol (engineering). Replaces
plan v2, which had one launch day for everyone.

## Stages

- [REQ-1] Release 4.3 rolls out in two stages: a beta release first, then the production release. Each stage needs its own go decision.
- [REQ-2] Stage one of the rollout: on Friday 24 October the beta release goes to the beta group only, about 40 customers who opted in to early access.
- [REQ-3] Stage two of the rollout: the production release goes to all customers on Wednesday 29 October, after the beta exit review approves it.
- [REQ-4] Beta exit review: on Tuesday 28 October Alice and Carol check the results of stage one. Stage two goes ahead only if no rollback was triggered and no blocker is open.

## Rollback

- [REQ-5] Rollback trigger: an error rate above 2% for 30 minutes, or any data-loss incident, in either stage.
- [REQ-6] Rollback call and target: Carol, as engineering lead, decides on a rollback. The target is the previous release, 4.2, restored within one hour.

## Monitoring

- [REQ-7] Beta monitoring: Carol's team checks error rates and support tickets from beta customers twice a day until the exit review.
