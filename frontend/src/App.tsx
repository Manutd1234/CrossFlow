import { useState } from 'react';
import { Header } from './components/Header';
import type { NavigationTab } from './components/Navigation';

export default function App() {
  const [activeTab, setActiveTab] = useState<NavigationTab>('congestion');

  return (
    <Header
      status="local-estimate"
      activeTab={activeTab}
      onTabChange={setActiveTab}
    />
  );
}
