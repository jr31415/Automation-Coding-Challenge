import express from "express";
import cookieParser from "cookie-parser";
import { randomUUID } from "node:crypto";
import { members, formatCents, type SubAccount } from "./data.js";
import { layout } from "./views/layout.js";
import { loginPage } from "./views/login.js";
import { searchPage } from "./views/search.js";
import { memberPage } from "./views/member.js";
import { newSubAccountPage } from "./views/newSubAccount.js";
import { confirmPage } from "./views/confirm.js";

const PORT = Number(process.env.MOCK_APP_PORT ?? 4000);

// Session store: sessionId -> { authenticated, createdAt, sessionTimeoutMs }
interface Session {
  authenticated: boolean;
  createdAt: number;
}
const sessions = new Map<string, Session>();

// Simulation knobs, settable via query/env for the discovery + replay demos.
const SESSION_TIMEOUT_MS = Number(process.env.MOCK_APP_SESSION_TIMEOUT_MS ?? 15 * 60 * 1000);

const app = express();
app.use(express.urlencoded({ extended: true }));
app.use(cookieParser());

function getSession(req: express.Request): Session | null {
  const sid = req.cookies.sid;
  if (!sid) return null;
  const s = sessions.get(sid);
  if (!s) return null;
  if (Date.now() - s.createdAt > SESSION_TIMEOUT_MS) {
    sessions.delete(sid);
    return null;
  }
  return s;
}

function requireAuth(req: express.Request, res: express.Response, next: express.NextFunction) {
  const s = getSession(req);
  if (!s || !s.authenticated) {
    res.redirect(`/login?next=${encodeURIComponent(req.originalUrl)}&reason=timeout`);
    return;
  }
  next();
}

app.get("/login", (req, res) => {
  const next = typeof req.query.next === "string" ? req.query.next : "/members/search";
  const reason = typeof req.query.reason === "string" ? req.query.reason : undefined;
  res.send(layout("Sign In — Riverbend Credit Union Admin", loginPage(next, reason)));
});

app.post("/login", (req, res) => {
  const { username, password, next } = req.body as { username?: string; password?: string; next?: string };
  // Deliberately permissive "auth" -- this is a mock, not a real credential check.
  if (!username || !password) {
    res.send(
      layout(
        "Sign In — Riverbend Credit Union Admin",
        loginPage(next || "/members/search", "invalid", "Username and password are required."),
      ),
    );
    return;
  }
  const sid = randomUUID();
  sessions.set(sid, { authenticated: true, createdAt: Date.now() });
  res.cookie("sid", sid, { httpOnly: true });
  res.redirect(next && next.startsWith("/") ? next : "/members/search");
});

app.post("/logout", (req, res) => {
  const sid = req.cookies.sid;
  if (sid) sessions.delete(sid);
  res.redirect("/login");
});

app.get("/members/search", requireAuth, (req, res) => {
  const q = typeof req.query.q === "string" ? req.query.q : "";
  res.send(layout("Member Search — Riverbend Credit Union Admin", searchPage(q)));
});

app.get("/members/lookup", requireAuth, (req, res) => {
  const q = typeof req.query.q === "string" ? req.query.q.trim() : "";
  res.redirect(`/members/${encodeURIComponent(q)}`);
});

app.get("/members/:id", requireAuth, (req, res) => {
  const member = members[req.params.id];
  if (!member) {
    res.status(200).send(
      layout(
        "Member Search — Riverbend Credit Union Admin",
        searchPage(req.params.id, `No member found with ID "${req.params.id}".`),
      ),
    );
    return;
  }
  res.send(layout(`Member ${member.id} — Riverbend Credit Union Admin`, memberPage(member)));
});

app.get("/members/:id/sub-account/new", requireAuth, (req, res) => {
  const member = members[req.params.id];
  if (!member) {
    res.status(404).send(layout("Not Found", `<p>No such member.</p>`));
    return;
  }
  if (member.status === "restricted") {
    res.status(200).send(
      layout(
        `Member ${member.id} — Riverbend Credit Union Admin`,
        `<div class="banner banner-error">Action denied: this member's account is restricted. Opening new sub-accounts is not permitted.</div>` +
          memberPage(member),
      ),
    );
    return;
  }
  res.send(
    layout(`New Sub-Account — Member ${member.id}`, newSubAccountPage(member, undefined)),
  );
});

app.post("/members/:id/sub-account/new", requireAuth, (req, res) => {
  const member = members[req.params.id];
  if (!member) {
    res.status(404).send(layout("Not Found", `<p>No such member.</p>`));
    return;
  }
  const { subAccountType, initialDepositDollars } = req.body as {
    subAccountType?: string;
    initialDepositDollars?: string;
  };

  const errors: string[] = [];
  if (!subAccountType) errors.push("Sub-account type is required.");
  const deposit = Number(initialDepositDollars);
  if (!initialDepositDollars || Number.isNaN(deposit)) {
    errors.push("Initial deposit must be a number.");
  } else if (deposit < 5) {
    errors.push("Initial deposit must be at least $5.00.");
  }

  if (errors.length > 0) {
    res.status(200).send(
      layout(
        `New Sub-Account — Member ${member.id}`,
        newSubAccountPage(member, errors, { subAccountType, initialDepositDollars }),
      ),
    );
    return;
  }

  // Occasionally simulate a transient backend hiccup so replay has to demonstrate retry/detection.
  const simulateBusy = req.query.simulateBusy === "1";
  if (simulateBusy) {
    res.status(503).send(layout("System Busy", `<div class="banner banner-error">System busy, please retry.</div>`));
    return;
  }

  const newSub: SubAccount = {
    type: subAccountType!,
    balanceCents: Math.round(deposit * 100),
  };
  member.subAccounts.push(newSub);

  const confirmationId = `SA-${Date.now().toString(36).toUpperCase()}`;
  res.send(
    layout(
      `Sub-Account Confirmed — Member ${member.id}`,
      confirmPage(member, newSub, confirmationId),
    ),
  );
});

app.get("/", (_req, res) => res.redirect("/members/search"));

app.listen(PORT, () => {
  console.log(`Mock back-office app listening on http://localhost:${PORT}`);
});
