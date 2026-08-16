import UploadPanel from "../components/UploadPanel";
import JobsPanel from "../components/JobsPanel";
import MatchesPanel from "../components/MatchesPanel";

export default function JobsAndMatches({ resume, setResume, toast }) {
  return (
    <main className="grid gap-5 lg:grid-cols-[400px_1fr]">
      <div className="space-y-5">
        <UploadPanel resume={resume} setResume={setResume} toast={toast} />
        <JobsPanel toast={toast} />
      </div>
      <MatchesPanel resume={resume} toast={toast} />
    </main>
  );
}
