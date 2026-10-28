# Assumptions & questions for OpsFleet

The brief invites questions. These are the ones we would send at kickoff, with the assumption the prototype makes until we hear back.

| # | Question | Assumption in the prototype |
|---|---|---|
| 1 | What does "products related to him" mean: brand ownership, category, department, or explicit product lists? | Additive grants by **brand, category and department** per user (`config/users.yaml`). Orders and customers are visible through in-scope items. |
| 2 | Which roles exist, and who can edit the persona? | `admin` (CEO and staff: all products, persona editing, all traces) and `analyst` (scoped). |
| 3 | Is customer-level output allowed at all (e.g. "top customers")? | Yes, by **pseudonymous user id** plus coarse attributes (country, channel). Never names, emails, addresses, phones or coordinates. Production adds a minimum group size of 5 for demographic breakdowns. |
| 4 | Are city/state considered sensitive? | Not PII on their own. Kept, but aggregated. Exact address and lat/long are removed. |
| 5 | Retention and undo for deleted reports? | Soft delete, `/undo` within 30 minutes, then hard purge after 30 days (production job). Audit log kept for 1 year. |
| 6 | Do reports belong to a user or a team? Can they be shared? | Owned by the creating user; users can delete only their own. Sharing is future work, with deletion staying owner-only. |
| 7 | Which languages must be supported? | Any. The guard is multilingual and the agent answers in the user's language. |
| 8 | Which definitions should be canonical (net revenue, churn, active customer)? | Taken from the golden trios: net revenue excludes Cancelled and Returned items; churn means 90 days without an order. |
| 9 | Fiscal calendar? | Calendar quarters (Q1 = Jan–Mar). |
| 10 | Expected scale (users, questions per day) and latency expectations? | Tens of executives, hundreds of questions per day; 10–30 s acceptable with progress feedback. |
| 11 | Data residency and model constraints (Vertex AI only? region?) | Prototype: either AI Studio or Vertex AI. Production: Vertex AI in the customer's region with VPC-SC. |
| 12 | Where should the "Golden" trios come from initially, and who curates them? | An analyst team owns a versioned GCS bucket. The prototype ships 11 hand-written trios. |
| 13 | Should the CEO's weekly tone change go live immediately or after review? | Immediately for admins, with preview, history and rollback. Production can add a second approver and an eval gate. |
