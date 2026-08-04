import ApplicationServices
import Foundation

enum AccessibilityMetadataProvider {
    static func windowTitle(processID: pid_t, prompt: Bool = false) -> String? {
        let options = ["AXTrustedCheckOptionPrompt": prompt] as CFDictionary
        guard AXIsProcessTrustedWithOptions(options) else { return nil }
        let application = AXUIElementCreateApplication(processID)
        var windowValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(application, kAXFocusedWindowAttribute as CFString, &windowValue) == .success,
              let windowValue else { return nil }
        var titleValue: CFTypeRef?
        let window = unsafeBitCast(windowValue, to: AXUIElement.self)
        guard AXUIElementCopyAttributeValue(window, kAXTitleAttribute as CFString, &titleValue) == .success else { return nil }
        return titleValue as? String
    }
}
