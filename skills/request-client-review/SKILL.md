---
name: request-client-review
description: >-
  Turn a real GitHub pull request into a client review on scopeblind.com,
  with a short brief, editable success criteria, a preview, exact approvals by
  both people, a receiver that applies only the approved version, and a
  separate acceptance of the recorded result. Use when work done for someone
  else's repository needs their sign-off, when a client must approve the exact
  change before it is applied, or when an agency wants a record of what was
  approved and applied.
license: MIT
compatibility: A GitHub pull request; the repository owner installs the receiver
metadata:
  author: ScopeBlind
  version: "0.1.0"
---

# Request a client review

The person who did the work writes a brief and the criteria; the client
reviews the exact change and a preview, both approve the same version, the
repository's own receiver applies only that version, and the client accepts
the recorded result separately. Every step is signed and the record can be
checked by anyone who is given it.

## Try it first, without your repository

<https://scopeblind.com/standard?trial=new&view=repository> opens a shared
button-fix demo in a ScopeBlind-owned disposable repository. It creates a real
pull request, a private invitation, an inspection by the repository workflow,
and the full approve, apply, accept path. No access to your repositories, no
account.

## For a real pull request

1. Open <https://scopeblind.com/standard?trial=new&view=repository&setup=own>.
2. Paste the pull request URL. Connect GitHub for this PR so the receiver can
   bring back its files, checks, and preview choices. Your GitHub credential
   stays with the repository.
3. Write the brief and the success criteria. Make each criterion something the
   reviewer can check. An agent's assessment does not decide a criterion.
4. Set the destination branch and the allowed files or folders. Changes outside
   that scope are outside the receiver's supported changes.
5. Install the receiver on the repository's trusted runner. Only its public key
   goes on the page; the private key and the GitHub token stay in the
   repository's secrets. The page prints the command, of the form:

   ```bash
   npx --yes protect-mcp@0.24.1 repository connect --link-stdin --install
   ```

6. Invite the reviewer with a private invitation, or select a reviewer in a
   client project. They join with a key generated in their browser; no GitHub
   account is needed to review.
7. Both people approve the exact inspected version. The receiver rechecks the
   destination before applying it. The client then accepts the recorded result,
   or requests changes, as a separate decision.

## What the record establishes, and does not

- Integrity: the signatures and relationships in the record check out.
- Authority: which keys approved which exact version.
- Observed effect: what the receiver observed through GitHub's API. This is
  the receiver's signed observation, not a GitHub-signed attestation.
- Recipient: whether the client accepted this exact result.

A preview link is mutable; an observed head association is not a frozen copy
of a website. Agent findings are recommendations. Signatures bind records to
keys, not to a person's legal identity.

## Keep the context for the next review

A client project keeps the repository connection, the people, and the review
history together, so the next request needs no new setup and returning
reviewers use their existing membership.
