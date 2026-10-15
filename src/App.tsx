import React from 'react';
import { TaskProvider } from './context/TaskContext';
import { AppShell } from './components/AppShell';
import { ErrorBoundary } from './components/ErrorBoundary';

export const AppContent: React.FC = () => {
  return <AppShell />;
};

export const App: React.FC = () => {
  return (
    <TaskProvider>
      <ErrorBoundary componentName="Application Root">
        <AppContent />
      </ErrorBoundary>
    </TaskProvider>
  );
};

export default App;
