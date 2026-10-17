"use client";

import { useEffect, useState } from "react";
import { LogoButton } from "./LogoButton";

export type NavTab = "home" | "product" | "cases" | "contact";

interface HeaderProps {
  activeTab: NavTab;
  onTabChange: (tab: NavTab) => void;
  onSignInClick: () => void;
}

const NAV_ITEMS: { id: NavTab; label: string }[] = [
  { id: "home", label: "Home" },
  { id: "product", label: "Product" },
  { id: "cases", label: "Case Studies" },
  { id: "contact", label: "Contact" },
];

export function Header({ activeTab, onTabChange, onSignInClick }: HeaderProps) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  // Close mobile menu on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && mobileMenuOpen) {
        setMobileMenuOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [mobileMenuOpen]);

  // Close mobile menu on resize to desktop (> 720px)
  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth > 720 && mobileMenuOpen) {
        setMobileMenuOpen(false);
      }
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [mobileMenuOpen]);

  // Lock body scroll when mobile menu is open
  useEffect(() => {
    if (mobileMenuOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [mobileMenuOpen]);

  const handleNavClick = (tab: NavTab) => {
    onTabChange(tab);
    setMobileMenuOpen(false);
  };

  return (
    <>
      <header className="header-wrapper">
        <div className="header-row">
          {/* Left Block: Logo */}
          <div className="header-left">
            <LogoButton
              size={42}
              onClick={() => handleNavClick("home")}
              className="sm:w-[42px] sm:h-[42px] w-[46px] h-[46px]"
            />
          </div>

          {/* Center Block: Desktop Navigation Pill (Hidden <= 720px) */}
          <nav
            aria-label="Primary Navigation"
            className="hidden md:flex items-center justify-center header-center"
          >
            <ul className="desktop-nav-pill m-0 p-0">
              {NAV_ITEMS.map((item) => {
                const isActive = activeTab === item.id;
                return (
                  <li key={item.id} className="relative">
                    <button
                      type="button"
                      onClick={() => handleNavClick(item.id)}
                      className={`nav-link-btn ${
                        isActive ? "opacity-100 font-semibold" : "opacity-60 hover:opacity-100"
                      }`}
                    >
                      <span>{item.label}</span>

                      {/* Three small black dots beneath active label */}
                      <span
                        className={`nav-dots transition-opacity duration-200 ${
                          isActive ? "opacity-100" : "opacity-0"
                        }`}
                        aria-hidden="true"
                      >
                        <span className="nav-dot" />
                        <span className="nav-dot" />
                        <span className="nav-dot" />
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </nav>

          {/* Right Block: Desktop Sign-In Action */}
          <div className="hidden md:flex items-center justify-end header-right">
            <button
              type="button"
              onClick={onSignInClick}
              className="sign-in-btn focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80"
            >
              Sign In
            </button>
          </div>

          {/* Mobile Menu Button (Visible <= 720px) */}
          <div className="md:hidden flex items-center justify-end">
            <button
              type="button"
              aria-label={mobileMenuOpen ? "Close navigation menu" : "Open navigation menu"}
              aria-expanded={mobileMenuOpen}
              onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
              className={`w-12 h-12 rounded-full flex flex-col items-center justify-center transition-colors duration-200 cursor-pointer shadow-[0_4px_14px_rgba(0,0,0,0.3)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white ${
                mobileMenuOpen ? "bg-white" : "bg-[#28282a]"
              }`}
            >
              <div className="w-5 h-3.5 relative flex flex-col justify-between">
                <span
                  className={`block h-[2px] w-full rounded-full transition-all duration-300 ${
                    mobileMenuOpen
                      ? "bg-black rotate-45 translate-y-[6px]"
                      : "bg-white"
                  }`}
                />
                <span
                  className={`block h-[2px] w-full rounded-full transition-all duration-300 ${
                    mobileMenuOpen ? "bg-black opacity-0" : "bg-white"
                  }`}
                />
                <span
                  className={`block h-[2px] w-full rounded-full transition-all duration-300 ${
                    mobileMenuOpen
                      ? "bg-black -rotate-45 -translate-y-[6px]"
                      : "bg-white"
                  }`}
                />
              </div>
            </button>
          </div>
        </div>
      </header>

      {/* Mobile Menu Overlay */}
      {mobileMenuOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Mobile Navigation"
          className="modal-overlay md:hidden"
        >
          {/* Backdrop with 62% black opacity & 6px blur */}
          <div
            onClick={() => setMobileMenuOpen(false)}
            className="modal-backdrop"
            style={{ backgroundColor: "rgba(0,0,0,0.65)", backdropFilter: "blur(6px)" }}
            aria-hidden="true"
          />

          {/* Mobile Menu Sheet */}
          <div className="mobile-menu-sheet">
            <div className="flex justify-between items-center mb-6 pb-4 border-b border-black/10">
              <span className="font-sans text-[11.5px] font-semibold text-black/50 tracking-wider uppercase">
                Node &amp; Nerve
              </span>
              <button
                type="button"
                onClick={() => setMobileMenuOpen(false)}
                className="text-xs text-black/60 hover:text-black font-semibold cursor-pointer py-1 px-2 rounded-full hover:bg-black/5 transition-colors"
              >
                ✕ Close
              </button>
            </div>

            <ul className="flex flex-col space-y-4 list-none m-0 p-0 mb-7">
              {NAV_ITEMS.map((item, idx) => {
                const isActive = activeTab === item.id;
                return (
                  <li key={item.id} style={{ animationDelay: `${0.04 * idx}s` }}>
                    <button
                      type="button"
                      onClick={() => handleNavClick(item.id)}
                      className={`w-full py-2.5 font-sans text-[17px] font-medium tracking-tight text-[#222224] transition-opacity duration-200 flex flex-col items-center justify-center cursor-pointer ${
                        isActive ? "opacity-100 font-semibold" : "opacity-75 hover:opacity-100"
                      }`}
                    >
                      <span>{item.label}</span>
                      {/* Active link three-dot indicator */}
                      {isActive && (
                        <span className="flex gap-1.5 items-center justify-center mt-1.5" aria-hidden="true">
                          <span className="w-1.5 h-1.5 rounded-full bg-black" />
                          <span className="w-1.5 h-1.5 rounded-full bg-black" />
                          <span className="w-1.5 h-1.5 rounded-full bg-black" />
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>

            {/* Mobile Sign In button */}
            <div className="pt-2 border-t border-black/5">
              <button
                type="button"
                onClick={() => {
                  setMobileMenuOpen(false);
                  onSignInClick();
                }}
                className="w-full bg-[#28282a] hover:bg-[#343437] text-white py-3.5 rounded-full text-[14.5px] font-medium transition-all duration-200 shadow-md cursor-pointer"
              >
                Sign In / Console Access
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
