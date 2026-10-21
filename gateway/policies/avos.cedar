// Avos authorization policy. Default deny: anything no `permit` matches is refused.
// Principal attributes (set by the gateway from the caller's SPIFFE ID):
//   kind: "vm" | "agent" | "sub"   name: last path segment   root: the top-level agent's name ("" for a VM)
// Top-level agent names are chosen by the operator (POST /spawn from the host); sub-agent names are chosen by
// their parent, so rights that matter are granted on kind == "agent" only.

@id("llm-all")
permit (principal, action == Action::"llm.call", resource);

@id("refund-support")
permit (principal, action == Action::"tool.call", resource == Tool::"issue_refund")
when { principal.kind == "agent" && principal.name == "support" && context.amount_cents <= 20000 };

@id("refund-manager")
permit (principal, action == Action::"tool.call", resource == Tool::"issue_refund")
when { principal.kind == "agent" && principal.name == "refund-manager" && context.amount_cents <= 100000 };

// Egress allowlist for net.connect (CONNECT through the gateway). Empty: no VM reaches the internet.
// Example: permit (principal, action == Action::"net.connect", resource == Host::"pypi.org:443");
