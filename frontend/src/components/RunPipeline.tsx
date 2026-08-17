import type { Run, RunStep } from "../lib/types";
import { StatusBadge } from "./StatusBadge";

const STAGES: RunStep["stage"][] = ["INGEST", "UNDERSTAND", "EXAMINE", "REVIEW", "COMMIT"];

export function RunPipeline({ run }: { run: Run }) {
  return (
    <ol className="pipeline" aria-label="Run stages">
      {STAGES.map((stage, index) => {
        const attempts = run.steps.filter((step) => step.stage === stage);
        const latest = [...attempts].sort((a, b) => b.attempt - a.attempt)[0];
        const status = latest?.status ?? "PENDING";
        return (
          <li className="pipeline-stage" key={stage}>
            <span className="pipeline-index">{index + 1}</span>
            <div>
              <strong>{stage}</strong>
              <div className="pipeline-meta">
                <StatusBadge value={status} />
                {latest ? <span>attempt {latest.attempt}</span> : <span>not started</span>}
              </div>
              {latest?.error_code ? <p className="stage-error">{latest.error_code}: {latest.error_message}</p> : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
