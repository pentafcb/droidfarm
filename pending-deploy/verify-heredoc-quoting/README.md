# verify-heredoc-quoting

`verify-live.sh --ssh` is the final gate of the morning new-box flow
(`sync-newbox.sh --yes` step 3). Its remote probe heredoc had a missing
closing quote that made every `--ssh` run report a bogus
`cron daemon is ''` warning, and a `systemctl is-active || echo unknown`
idiom that leaked stray `unknown` lines. Both fixed; T38 now runs the real
heredoc through a real `sh -s` so this bug class can never slip through the
canned-ssh fakes again.

See DEPLOY.md for the full write-up.
