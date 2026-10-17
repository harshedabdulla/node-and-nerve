"use client";

import { useEffect, useRef, useState } from "react";

interface BackgroundVideoProps {
  videoUrl?: string;
}

export function BackgroundVideo({
  videoUrl = "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260809_012548_ef22562c-c0ae-4816-ad9d-f8922af4e6a7.mp4",
}: BackgroundVideoProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [prefersReduced, setPrefersReduced] = useState(false);

  useEffect(() => {
    // Detect prefers-reduced-motion
    const mediaQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
    setPrefersReduced(mediaQuery.matches);

    const handler = (e: MediaQueryListEvent) => setPrefersReduced(e.matches);
    mediaQuery.addEventListener("change", handler);
    return () => mediaQuery.removeEventListener("change", handler);
  }, []);

  useEffect(() => {
    if (videoRef.current) {
      if (prefersReduced) {
        videoRef.current.pause();
      } else {
        videoRef.current.play().catch(() => {
          // Autoplay policy fallback
        });
      }
    }
  }, [prefersReduced]);

  return (
    <div className="fixed inset-0 w-full h-full -z-10 bg-black overflow-hidden pointer-events-none select-none">
      {/* Background Video */}
      {!prefersReduced && (
        <video
          ref={videoRef}
          src={videoUrl}
          autoPlay
          muted
          loop
          playsInline
          aria-hidden="true"
          className="absolute inset-0 w-full h-full object-cover transition-opacity duration-700 opacity-90"
        />
      )}

      {/* Subtle cinematic contrast overlay to ensure WCAG readability without dimming video vibrance */}
      <div
        className="absolute inset-0 bg-gradient-to-b from-black/55 via-black/20 to-black/75 pointer-events-none"
        aria-hidden="true"
      />
      {/* Soft radial vignette for central focus */}
      <div
        className="absolute inset-0 bg-[radial-gradient(circle_at_center,transparent_40%,rgba(0,0,0,0.5)_100%)] pointer-events-none"
        aria-hidden="true"
      />
    </div>
  );
}
