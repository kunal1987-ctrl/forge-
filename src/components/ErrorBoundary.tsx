import React, { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw, LayoutDashboard, ChevronDown, ChevronRight } from 'lucide-react';

export interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode | ((props: { error: Error | null; resetError: () => void }) => ReactNode);
  onReset?: () => void;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  componentName?: string;
  showDetails?: boolean;
}

export interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
  isDetailsOpen: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
    errorInfo: null,
    isDetailsOpen: false,
  };

  public static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return {
      hasError: true,
      error,
    };
  }

  public override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    this.setState({ errorInfo });
    // Defensive logging without throwing
    try {
      console.error(`[ErrorBoundary] Caught error in component "${this.props.componentName || 'Unknown'}":`, error, errorInfo);
    } catch {
      // ignore
    }

    if (this.props.onError) {
      try {
        this.props.onError(error, errorInfo);
      } catch (err) {
        console.error('[ErrorBoundary] Error during onError callback:', err);
      }
    }
  }

  public resetError = (): void => {
    this.setState({
      hasError: false,
      error: null,
      errorInfo: null,
      isDetailsOpen: false,
    });

    if (this.props.onReset) {
      try {
        this.props.onReset();
      } catch (err) {
        console.error('[ErrorBoundary] Error during onReset callback:', err);
      }
    }
  };

  private toggleDetails = (): void => {
    this.setState((prev) => ({ isDetailsOpen: !prev.isDetailsOpen }));
  };

  public override render(): ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) {
        if (typeof this.props.fallback === 'function') {
          return this.props.fallback({
            error: this.state.error,
            resetError: this.resetError,
          });
        }
        return this.props.fallback;
      }

      const componentLabel = this.props.componentName ? ` "${this.props.componentName}"` : '';
      const errorMessage = this.state.error?.message || 'An unexpected rendering error occurred in this view.';
      const componentStack = this.state.errorInfo?.componentStack || '';

      return (
        <div className="w-full max-w-2xl mx-auto my-8 p-6 bg-white border border-rose-200 rounded-2xl shadow-sm text-zinc-900 font-sans animate-in fade-in">
          <div className="flex items-start gap-4">
            <div className="p-3 bg-rose-50 border border-rose-200 rounded-xl text-rose-600 shrink-0">
              <AlertTriangle className="h-6 w-6" />
            </div>

            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-base font-semibold text-zinc-900">
                  Component Error{componentLabel}
                </h3>
                <span className="font-mono text-[10px] px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200 font-bold uppercase tracking-wider">
                  Crash Prevented
                </span>
              </div>

              <p className="text-xs text-zinc-600 mt-1.5 leading-relaxed">
                The component encountered an error during rendering. The application state has been preserved, and you can attempt to reload this view.
              </p>

              {/* Error Message Box */}
              <div className="mt-3.5 p-3 rounded-lg bg-zinc-50 border border-zinc-200 text-xs font-mono text-rose-700 break-words leading-relaxed">
                {errorMessage}
              </div>

              {/* Collapsible Stack Trace */}
              {componentStack && (
                <div className="mt-3">
                  <button
                    type="button"
                    onClick={this.toggleDetails}
                    className="flex items-center gap-1.5 text-[11px] font-mono text-zinc-500 hover:text-zinc-800 transition"
                  >
                    {this.state.isDetailsOpen ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                    <span>{this.state.isDetailsOpen ? 'Hide stack details' : 'Show stack details'}</span>
                  </button>

                  {this.state.isDetailsOpen && (
                    <pre className="mt-2 p-3 bg-zinc-900 text-zinc-200 text-[10px] font-mono rounded-lg overflow-x-auto max-h-48 border border-zinc-800">
                      {componentStack}
                    </pre>
                  )}
                </div>
              )}

              {/* Action Buttons */}
              <div className="mt-5 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  onClick={this.resetError}
                  className="inline-flex items-center gap-2 px-4 py-2 bg-zinc-900 hover:bg-zinc-800 text-white rounded-xl text-xs font-medium shadow-xs transition active:scale-[0.98]"
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  <span>Reload Component</span>
                </button>

                {this.props.onReset && (
                  <button
                    type="button"
                    onClick={this.props.onReset}
                    className="inline-flex items-center gap-2 px-4 py-2 bg-zinc-100 hover:bg-zinc-200 text-zinc-700 rounded-xl text-xs font-medium border border-zinc-200 transition"
                  >
                    <LayoutDashboard className="h-3.5 w-3.5" />
                    <span>Return to Safe State</span>
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

/**
 * Functional HOC wrapper for ErrorBoundary
 */
export function withErrorBoundary<P extends object>(
  WrappedComponent: React.ComponentType<P>,
  boundaryProps?: Omit<ErrorBoundaryProps, 'children'>
): React.FC<P> {
  const ComponentWithErrorBoundary: React.FC<P> = (props) => (
    <ErrorBoundary {...boundaryProps}>
      <WrappedComponent {...props} />
    </ErrorBoundary>
  );

  ComponentWithErrorBoundary.displayName = `withErrorBoundary(${
    WrappedComponent.displayName || WrappedComponent.name || 'Component'
  })`;

  return ComponentWithErrorBoundary;
}

export default ErrorBoundary;
