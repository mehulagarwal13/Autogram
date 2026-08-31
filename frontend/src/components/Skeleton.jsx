// Shape-matched loading placeholders — swap in wherever the eventual layout
// is already known, instead of blocking the whole view behind a spinner.
export function SkeletonTile({ className = "" }) {
  return (
    <div className={`card p-4 ${className}`}>
      <div className="skeleton h-4 w-4 rounded" />
      <div className="skeleton mt-3 h-6 w-12 rounded" />
      <div className="skeleton mt-2 h-3 w-20 rounded" />
    </div>
  );
}

export function SkeletonRow({ className = "" }) {
  return (
    <div className={`flex items-center justify-between gap-3 px-5 py-3.5 ${className}`}>
      <div className="min-w-0 flex-1 space-y-1.5">
        <div className="skeleton h-3.5 w-40 rounded" />
        <div className="skeleton h-3 w-24 rounded" />
      </div>
      <div className="skeleton h-5 w-16 shrink-0 rounded-md" />
    </div>
  );
}

export function SkeletonLine({ width = "w-full", className = "" }) {
  return <div className={`skeleton h-3.5 ${width} rounded ${className}`} />;
}
