# Testing Checklist for Post-Launch Polish

## Prerequisites
- [ ] watercooler-cloud on `post-launch-polish` branch
- [ ] watercooler-site on `main` branch
- [ ] Python 3.10+ environment active
- [ ] Node.js installed for watercooler-site

## watercooler-cloud Tests

### Automated Tests
- [ ] `pytest tests/ -v` passes (103 passed, 2 skipped)
- [ ] No new warnings or errors

### stdio Transport (Backward Compatibility)
- [ ] `python3 -m watercooler_mcp` starts without error
- [ ] Can connect from Claude Code/Cursor with stdio config
- [ ] `watercooler_health` tool works
- [ ] `watercooler_list_threads` works
- [ ] `watercooler_say` creates entries

### HTTP Transport (New Feature)
- [ ] `./scripts/mcp-server-daemon.sh start` succeeds
- [ ] `./scripts/mcp-server-daemon.sh status` shows running
- [ ] Server runs on http://127.0.0.1:3000/mcp
- [ ] Logs appear in ~/.watercooler/mcp-server.log
- [ ] `./scripts/mcp-server-daemon.sh stop` gracefully stops
- [ ] Can connect from Claude Code with HTTP config
- [ ] All MCP tools work over HTTP

### Bug Fixes (from onboarding-mcp-bugfixes)
- [ ] No IterableList.origin errors
- [ ] HTTPS auto-provisioning works
- [ ] Git operations succeed with GITHUB_TOKEN

### Installation (uvx)
- [ ] `git push origin post-launch-polish` (if not done)
- [ ] `uvx --from "git+https://github.com/mostlyharmless-ai/watercooler-cloud@post-launch-polish" watercooler-mcp` works
- [ ] OR merge to main and test: `uvx --from git+https://github.com/mostlyharmless-ai/watercooler-cloud watercooler-mcp`

## watercooler-site Tests

### Build & Run
- [ ] `npm run build` succeeds
- [ ] No TypeScript errors
- [ ] `npm run dev` starts

### Multi-Org Support (New Feature)
- [ ] Settings page loads at /settings
- [ ] Organizations section appears
- [ ] Personal account shows as "Always Enabled"
- [ ] Org list fetches from GitHub API
- [ ] Toggle switches work for each org
- [ ] "Re-authorize GitHub" button appears
- [ ] Re-auth flow redirects to GitHub OAuth
- [ ] New orgs appear after re-authorization

### Database
- [ ] Prisma migration applied successfully
- [ ] UserOrganization table exists
- [ ] Can query: `psql $DATABASE_URL -c "SELECT * FROM \"UserOrganization\""`

### Auth Callback
- [ ] Sign in triggers org sync
- [ ] Organizations upserted to database
- [ ] No errors in server logs during auth

## Integration Tests

### End-to-End Flow
- [ ] New user signs up
- [ ] OAuth authorizes multiple orgs
- [ ] Dashboard shows repos from all authorized orgs
- [ ] MCP server can access threads repos
- [ ] Can create/read/update threads
- [ ] HTTP transport works alongside stdio

### Backward Compatibility
- [ ] Existing users not affected
- [ ] Default stdio transport unchanged
- [ ] No breaking changes to MCP tool signatures
- [ ] Existing client configs still work

## Documentation

### README
- [ ] Installation instructions accurate
- [ ] uvx commands work as documented
- [ ] Links to setup guides valid

### New Docs
- [ ] `docs/http-transport.md` accurate
- [ ] Code examples work
- [ ] Environment variables documented

## Known Issues / Limitations
- [ ] Document any issues found
- [ ] Create GitHub issues for bugs
- [ ] Update roadmap if needed

## Sign-Off
- [ ] All tests pass
- [ ] No regressions found
- [ ] Ready to merge to main
- [ ] Ready for production deployment

---

**Tested by:** _____________________
**Date:** _____________________
**Notes:**
