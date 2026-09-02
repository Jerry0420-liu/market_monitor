# Release checklist

- [x] MIT `LICENSE` is present.
- [ ] Owner confirms market-data rights and provider activation requirements.
- [ ] Optional notification endpoint is reviewed and remains disabled until intentionally enabled.
- [ ] `python scripts/dev.py verify` passes.
- [ ] A clean-copy install and verification pass.
- [ ] Local startup, OWNER access, restart, and shutdown pass.
- [ ] Backup and restore pass without replaying old real-time notifications.
- [ ] No real secret, public bind, extra worker, or unapproved provider is present.
