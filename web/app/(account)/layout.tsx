import { ClerkProvider } from "@clerk/nextjs";

// Clerk loads only for sign-in, sign-up and the app; the public pages stay free of it.
const appearance = {
  variables: {
    colorPrimary: "#ffffff",
    colorPrimaryForeground: "#000000",
    colorBackground: "#0a0d18",
    colorForeground: "#ffffff",
    colorMutedForeground: "#8f9bc2",
    colorInput: "#10131f",
    colorInputForeground: "#ffffff",
    colorBorder: "#262c44",
    colorNeutral: "#c8d0ec",
    colorDanger: "#e8796b",
    colorSuccess: "#8fd19e",
    colorWarning: "#e3a93c",
    colorRing: "#7091e6",
    borderRadius: "12px",
    fontFamily: "var(--font-sans-family), Inter, system-ui, sans-serif",
  },
};

export default function AccountLayout({ children }: { children: React.ReactNode }) {
  return (
    <ClerkProvider
      appearance={appearance}
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
      signInFallbackRedirectUrl="/app"
      signUpFallbackRedirectUrl="/app"
    >
      {children}
    </ClerkProvider>
  );
}
