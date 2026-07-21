import { useEffect, useRef, useState } from "react";
import { ArrowIcon, CheckIcon, PrivacyIcon, SourceIcon, SparkIcon } from "./icons";
import type { BackendHealth } from "./transport";

type OnboardingProps = {
  backend: BackendHealth;
  onComplete: (sourceChoice: "demo" | "akahu" | "plaid" | "csv", file: File | null) => Promise<void>;
};

export function Onboarding({ backend, onComplete }: OnboardingProps) {
  const [sourceChoice, setSourceChoice] = useState<"demo" | "akahu" | "plaid" | "csv">("demo");
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    titleRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
      ));
      const first = focusable.at(0);
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && (document.activeElement === first || !focusable.includes(document.activeElement as HTMLElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const handleContinue = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onComplete(sourceChoice, csvFile);
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "Folio could not open this workspace. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="onboarding-layer" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
      <div ref={dialogRef} className="onboarding-shell onboarding-conversation-shell onboarding-entry-shell">
        <header className="onboarding-topbar">
          <div className="onboarding-brand" aria-label="Folio">
            <span aria-hidden="true">F.</span>
            <strong>Folio</strong>
          </div>
          <p className="onboarding-private-state"><PrivacyIcon size={14} /> Local finance workspace</p>
        </header>

        <main className="onboarding-main onboarding-conversation onboarding-entry-main">
          <section className="onboarding-turn" aria-live="polite">
            <div className="onboarding-agent-mark" aria-hidden="true">F</div>
            <div className="onboarding-turn-content">
              <div className="onboarding-panel">
                <p className="step-label">Start with the facts</p>
                <h1 id="onboarding-title" ref={titleRef} tabIndex={-1}>What should I look at first?</h1>
                <p className="panel-lede">Open Folio with a private example business, or bring in a local bank export. Once it is ready, I’ll ask the single most useful question in the conversation.</p>

                <div className="choice-grid onboarding-choice-list" role="group" aria-label="Starting finance source">
                  <button
                    type="button"
                    className={`choice-card ${sourceChoice === "demo" ? "is-selected" : ""}`}
                    aria-pressed={sourceChoice === "demo"}
                    onClick={() => setSourceChoice("demo")}
                  >
                    <span className="choice-icon"><SparkIcon /></span>
                    <span><strong>Open Folio demo</strong><small>Explore a fictional New Zealand business with sample transactions and open questions.</small></span>
                    <i aria-hidden="true">{sourceChoice === "demo" ? <CheckIcon size={14} /> : null}</i>
                  </button>

                  <button
                    type="button"
                    className={`choice-card ${sourceChoice === "akahu" ? "is-selected" : ""}`}
                    aria-pressed={sourceChoice === "akahu"}
                    disabled={backend.mode !== "live"}
                    onClick={() => setSourceChoice("akahu")}
                  >
                    <span className="choice-icon"><SourceIcon /></span>
                    <span>
                      <strong>{backend.akahuReady ? "Sync Akahu read-only" : "Preview an Akahu import"}</strong>
                      <small>{backend.akahuReady
                        ? "Bring settled New Zealand bank transactions into Folio through this computer."
                        : "Try the Akahu import flow with six fictional New Zealand transactions."}</small>
                    </span>
                    <i aria-hidden="true">{sourceChoice === "akahu" ? <CheckIcon size={14} /> : null}</i>
                  </button>

                  <button
                    type="button"
                    className={`choice-card ${sourceChoice === "plaid" ? "is-selected" : ""}`}
                    aria-pressed={sourceChoice === "plaid"}
                    disabled={backend.mode !== "live"}
                    onClick={() => setSourceChoice("plaid")}
                  >
                    <span className="choice-icon"><SourceIcon /></span>
                    <span>
                      <strong>{backend.plaidReady ? "Sync Plaid sandbox read-only" : "Preview a Plaid import"}</strong>
                      <small>{backend.plaidReady
                        ? "Bring settled US sandbox transactions into Folio through this computer."
                        : "Try the Plaid import flow with six fictional US transactions."}</small>
                    </span>
                    <i aria-hidden="true">{sourceChoice === "plaid" ? <CheckIcon size={14} /> : null}</i>
                  </button>

                  <button
                    type="button"
                    className={`choice-card ${sourceChoice === "csv" ? "is-selected" : ""}`}
                    aria-pressed={sourceChoice === "csv"}
                    onClick={() => setSourceChoice("csv")}
                  >
                    <span className="choice-icon"><SourceIcon /></span>
                    <span><strong>Choose a local CSV</strong><small>Import an exact Folio-format NZD export without sending the raw file to a cloud model.</small></span>
                    <i aria-hidden="true">{sourceChoice === "csv" ? <CheckIcon size={14} /> : null}</i>
                  </button>
                </div>

                {sourceChoice === "csv" ? (
                  <label className="csv-picker">
                    <span>Choose your Folio-format CSV</span>
                    <input
                      type="file"
                      accept=".csv,text/csv"
                      onChange={(event) => setCsvFile(event.currentTarget.files?.[0] ?? null)}
                    />
                    <small>{csvFile
                      ? `${csvFile.name} is ready for exact-template validation.`
                      : "Required columns: source_row_id, account_id, occurred_on, description, amount_minor, currency, status, external_reference."}</small>
                  </label>
                ) : null}

                {sourceChoice === "akahu" ? (
                  <div className="onboarding-callout onboarding-provider-callout">
                    <SourceIcon size={16} />
                    <p>{backend.akahuReady
                      ? <><strong>Akahu is configured.</strong> Folio will read accounts and settled transactions only. It cannot make payments or change your bank.</>
                      : <><strong>Preview with sample data.</strong> Folio will import six fictional Akahu-shaped transactions without contacting a bank. Add a Personal App or accredited OAuth connection to use your own account.</>}</p>
                  </div>
                ) : null}

                {sourceChoice === "plaid" ? (
                  <div className="onboarding-callout onboarding-provider-callout">
                    <SourceIcon size={16} />
                    <p>{backend.plaidReady
                      ? <><strong>Plaid sandbox is configured.</strong> Folio will create a Link token or sync settled sandbox transactions read-only. Access tokens are not stored.</>
                      : <><strong>Preview with sample data.</strong> Folio will import six fictional Plaid-shaped transactions without contacting a bank. To use Plaid's sandbox, enable <code>FINANCE_PLAID_ENABLED</code> and add your Plaid credentials.</>}</p>
                  </div>
                ) : null}

                <p className="onboarding-market-note">Plaid Link is for supported US, Canadian, UK and European institutions. Plaid does not support New Zealand banks, so Folio routes New Zealand owners to Akahu or local statements.</p>

                <div className="onboarding-callout onboarding-privacy-callout">
                  <PrivacyIcon size={16} />
                  <p><strong>Your records stay on this computer.</strong> Language-model choices are available later in Privacy &amp; models. Finance calculations and source history remain local.</p>
                </div>

                {backend.mode === "degraded" || backend.mode === "offline" ? (
                  <div className="onboarding-callout onboarding-error" role="status">
                    <strong>{backend.mode === "offline" ? "Folio is offline" : "The local service needs attention"}</strong>
                    <p>{backend.detail} Nothing will be substituted or saved until a real workspace is ready.</p>
                  </div>
                ) : null}
              </div>
            </div>
          </section>

          {submitError ? <div className="onboarding-callout onboarding-error" role="alert"><strong>Workspace was not opened</strong><p>{submitError}</p></div> : null}

          <footer className="onboarding-actions onboarding-entry-actions">
            <span><PrivacyIcon size={14} /> {backend.mode === "live" ? "Local service connected" : backend.mode === "fixture" ? "Sealed demo available" : backend.mode === "checking" ? "Finding local service…" : "Last view only"}</span>
            <button
              type="button"
              className="button button-primary"
              disabled={submitting || (sourceChoice === "csv" && !csvFile)}
              onClick={() => void handleContinue()}
            >
              {submitting
                ? "Preparing locally…"
                : sourceChoice === "demo"
                  ? "Open Folio demo"
                  : sourceChoice === "akahu"
                    ? backend.akahuReady ? "Sync Akahu read-only" : "Import sample Akahu data"
                    : sourceChoice === "plaid"
                      ? backend.plaidReady ? "Sync Plaid sandbox read-only" : "Import sample Plaid data"
                      : "Import and continue"}<ArrowIcon size={15} />
            </button>
          </footer>
        </main>
      </div>
    </div>
  );
}
