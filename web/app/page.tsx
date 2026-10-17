"use client";

import { useState } from "react";
import { BackgroundVideo } from "@/components/BackgroundVideo";
import { Header, NavTab } from "@/components/Header";
import { Hero } from "@/components/Hero";
import { StatisticsFooter } from "@/components/StatisticsFooter";
import { ProductModal } from "@/components/ProductModal";

export default function Home() {
  const [activeTab, setActiveTab] = useState<NavTab>("home");
  const [activeModal, setActiveModal] = useState<NavTab | "signin" | "explore" | null>(null);

  const handleTabChange = (tab: NavTab) => {
    setActiveTab(tab);
    if (tab !== "home") {
      setActiveModal(tab);
    }
  };

  const handleSignInClick = () => {
    setActiveModal("signin");
  };

  const handleExploreClick = () => {
    setActiveModal("explore");
  };

  const handleModalClose = () => {
    setActiveModal(null);
    setActiveTab("home");
  };

  return (
    <main className="relative w-full h-[100dvh] max-h-[100dvh] flex flex-col justify-between overflow-hidden bg-black text-white select-none">
      {/* 1. Full-bleed Background Video */}
      <BackgroundVideo />

      {/* 2. Top Centered Header */}
      <Header
        activeTab={activeTab}
        onTabChange={handleTabChange}
        onSignInClick={handleSignInClick}
      />

      {/* 3. Centered Hero Composition */}
      <Hero
        onExplore={handleExploreClick}
        onTrustClick={() => setActiveModal("product")}
      />

      {/* 4. Statistics Footer */}
      <StatisticsFooter
        onItemClick={() => setActiveModal("product")}
      />

      {/* 5. Interactive Details Modal */}
      <ProductModal
        view={activeModal}
        onClose={handleModalClose}
      />
    </main>
  );
}
