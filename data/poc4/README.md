# POC 4 results

`just poc4` on the dev host (c8i.large, nested virt), 2026-10-21, VMs `vm-01-76e04f` and `vm-02-6747db`. Design:
`docs/architecture.md` §4. The host gateway decides every model call, tool call and outbound connection with Cedar
(`gateway/policies/avos.cedar`). It identifies the caller by its own SVID over mTLS and attaches the model API key
only to allowed calls. The key is no longer in any VM.

| Done test | Result |
|---|---|
| 1. Same tool call, two identities | `issue_refund` $480: `agent/support` denied (`default-deny`), `agent/refund-manager` allowed (`refund-manager`). $150 for support allowed (`refund-support`); $1,500 for the manager denied. |
| 2. Denials attributable (M5) | **5/5 = 100%**: every denial carries principal, policy IDs and arguments, in the 403 and in its `authz.decision` row |
| 3. Permissions for life, no inheritance | The manager's calls after 70 s (past its 60 s SVID TTL) get the same decisions. Its sub-agent, also named `refund-manager`, is denied (`kind == "agent"` only). An agent calling the registrar is refused at the TLS handshake (no VM SVID). An agent asking for a top-level spawn gets `top-level spawns come from the host`. |
| 4. Borrowed SVID | Another principal reading it: `Permission denied`. A copy used from vm-02: 403 `vm-binding`. The copy after its TTL: refused at the TLS handshake. |
| 5. Again 30 min later | Run at 14:19 and 14:50 UTC on 2026-10-21 (VMs `vm-01-5df5c4`, `vm-01-a6c994`): all checks pass both times, with the same 9 tool and network decisions and the same policy IDs. M5 6/6 = 100% in each run (the `vm-binding` denial counted too). 0 VMs and 0 SPIRE entries left after each. |

- **Egress:** a principal connecting straight to `1.1.1.1:443` is blocked (the host does not route guests). A
  `CONNECT` through the gateway gets 403 `default-deny` (`net.connect Host::"1.1.1.1:443"`). The VM's own root
  has no route out either.
- **No key in the VM:** the MMDS listing has no `*_KEY` entry. The host refuses to boot a VM if `agent.env` holds one.
- **Cost of the extra hop:** 20 tool calls through forwarder, mTLS, Cedar and the ledger took a median of 15 ms
  (worst 30 ms, new connection each time); the test-5 runs measured 7 and 11 ms. The guest agent process (shim and
  forwarder) uses 25 MB RSS.

## Regression with the gateway in the path

- `just poc1`: passes twice; M1 2.7–2.9 s (`just bench 5`: median 2731 ms).
- `just poc3 20`: 6/6; M3 21/21; M4 median 1235 ms.
- `just poc2 10`: M2 10/10. M13 median 39.2 s, worst 63.3 s (39.3 s / 71.4 s before the gateway). Reward 2/10.
- `just job mini-swe-agent coding/slugify`: done, original tests pass, 83 s (345 s in POC 2, a different trajectory).
- Seen by the gateway over those 10 τ² jobs: a median of 15.5 model calls per job (9–20), with 75k tokens
  (39k–125k). Upstream latency was 1.46 s median and 7.0 s p95. That gave 154 decisions and 0 denials, all from
  `agent/tau2`. Token counts per principal are now recorded for cost per task (M12).

## Found on the way

- **No per-uid firewall in the guest.** The Firecracker CI guest kernel has legacy iptables, but no `owner` match.
  The design instead closes the one hole that mattered: the registrar trusted any caller from the VM's IP. It now
  requires the VM's own SVID (root-only) over mTLS, and top-level spawns accept only the host.
- **SPIRE entries leaked, one per VM (since POC 3).** A join token minted with `-spiffeID` creates an alias entry
  parented on the SPIRE agent. Evicting the agent left that entry behind. `vm-down` now deletes it, and 3 boots
  leave 0 entries. POC 3's "0 entries left" was wrong, because the alias entries were not counted.
- **Guests have no DNS.** With no route out, the image's `nameserver 1.1.1.1` made every lookup wait 10 s. Python's
  `HTTPServer` does one at bind, so M1 rose from 2.7 s to 12.8 s. The image now ships an empty `resolv.conf`.
