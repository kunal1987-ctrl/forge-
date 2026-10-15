import React, { useCallback, useState } from 'react';
import {
  LayoutDashboard,
  PlusSquare,
  Activity as ActivityIcon,
  GitPullRequest,
} from 'lucide-react';

export type NavigationTab = 'workspace' | 'new-task' | 'activity' | 'reviews';
export type LegacyView = 'new' | 'review';
export type ViewState = NavigationTab | LegacyView | string;

/**
 * Normalizes any view string, legacy alias, or undefined/null value
 * into a safe, canonical NavigationTab. Always defaults to 'workspace'.
 */
export function normalizeView(rawView?: string | null): NavigationTab {
  if (!rawView || typeof rawView !== 'string') {
    return 'workspace';
  }

  const trimmed = rawView.toLowerCase().trim();
  if (
    trimmed === 'new-task' ||
    trimmed === 'new' ||
    trimmed === 'new_task' ||
    trimmed === 'newtask' ||
    trimmed === 'create'
  ) {
    return 'new-task';
  }
  if (trimmed === 'activity' || trimmed === 'logs' || trimmed === 'events' || trimmed === 'telemetry') {
    return 'activity';
  }
  if (
    trimmed === 'reviews' ||
    trimmed === 'review' ||
    trimmed === 'diff' ||
    trimmed === 'gate' ||
    trimmed === 'pr'
  ) {
    return 'reviews';
  }
  if (trimmed === 'workspace' || trimmed === 'dashboard' || trimmed === 'home') {
    return 'workspace';
  }

  // Safe fallback to 'workspace' for any unrecognized or corrupted view state
  return 'workspace';
}

export interface NavigationProps {
  activeView?: string | null;
  onNavigate: (view: NavigationTab) => void;
  onClearErrors?: () => void;
  className?: string;
  hasReviewGate?: boolean;
}

/**
 * Custom React Hook providing safe, defensive view switching and form error clearing.
 */
export function useDefensiveNavigation(
  initialView: string = 'workspace',
  onViewChange?: (view: NavigationTab) => void
) {
  const [activeView, setActiveViewRaw] = useState<NavigationTab>(() => normalizeView(initialView));
  const [formErrors, setFormErrors] = useState<Record<string, string> | null>(null);

  const setActiveView = useCallback(
    (nextView?: string | null) => {
      const safeView = normalizeView(nextView);
      // Explicitly clear stale task form errors when switching views
      setFormErrors(null);
      setActiveViewRaw(safeView);

      if (onViewChange) {
        try {
          onViewChange(safeView);
        } catch (err) {
          console.warn('[useDefensiveNavigation] View change callback error:', err);
        }
      }
    },
    [onViewChange]
  );

  const clearFormErrors = useCallback(() => {
    setFormErrors(null);
  }, []);

  return {
    activeView,
    setActiveView,
    formErrors,
    setFormErrors,
    clearFormErrors,
  };
}

export const Navigation: React.FC<NavigationProps> = ({
  activeView,
  onNavigate,
  onClearErrors,
  className = '',
  hasReviewGate = false,
}) => {
  const currentSafeTab = normalizeView(activeView);

  const handleTabClick = (targetTab: NavigationTab) => {
    // Clear any stale task form errors when navigating
    if (onClearErrors) {
      try {
        onClearErrors();
      } catch (err) {
        console.warn('[Navigation] Failed to clear form errors:', err);
      }
    }

    const safeTarget = normalizeView(targetTab);
    if (typeof onNavigate === 'function') {
      onNavigate(safeTarget);
    }
  };

  const navItems: Array<{
    id: NavigationTab;
    label: string;
    icon: React.ComponentType<{ className?: string }>;
    badge?: string | boolean;
  }> = [
    {
      id: 'workspace',
      label: 'Workspace',
      icon: LayoutDashboard,
    },
    {
      id: 'new-task',
      label: 'New Task',
      icon: PlusSquare,
    },
    {
      id: 'activity',
      label: 'Activity',
      icon: ActivityIcon,
    },
    {
      id: 'reviews',
      label: 'Reviews',
      icon: GitPullRequest,
      badge: hasReviewGate,
    },
  ];

  return (
    <nav
      className={`sticky bottom-0 z-40 border-t border-zinc-200 bg-white/90 backdrop-blur-md ${className}`}
      role="navigation"
      aria-label="Main Navigation"
    >
      <div className="mx-auto flex max-w-lg items-center justify-around px-4 py-2 sm:py-2.5">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentSafeTab === item.id;

          return (
            <button
              key={item.id}
              type="button"
              id={`nav-tab-${item.id}`}
              onClick={() => handleTabClick(item.id)}
              className={`relative flex flex-col items-center gap-1 rounded-lg px-4 py-1.5 text-xs font-medium transition active:scale-[0.98] ${
                isActive
                  ? 'bg-zinc-100 text-zinc-950 font-semibold shadow-2xs'
                  : 'text-zinc-500 hover:text-zinc-800 hover:bg-zinc-50'
              }`}
              aria-current={isActive ? 'page' : undefined}
            >
              <div className="relative">
                <Icon className={`h-4 w-4 ${isActive ? 'text-zinc-950' : 'text-zinc-500'}`} />
                {item.badge && (
                  <span className="absolute -top-1 -right-1.5 flex h-2 w-2">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
                  </span>
                )}
              </div>
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>
    </nav>
  );
};

export default Navigation;
