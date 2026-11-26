export const hasPortableOnboarding = document.documentElement.hasAttribute("data-jaro-portable-dictation")
  && document.documentElement.hasAttribute("data-jaro-desktop-client")

export async function changePortableOnboarding(action: "restart" | "finish" | "microphone", id?: string) {
  const response = await fetch("/api/portable/onboarding", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, id }),
  })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Could not change dictation setup.")
  return result as { id: string | null }
}
