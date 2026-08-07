import AppKit
import XCTest
@testable import MeetingCapture

final class HUDLayoutTests: XCTestCase {
    private let screenFrame = NSRect(x: 0, y: 0, width: 2_624, height: 1_696)
    private let leftArea = NSRect(x: 0, y: 1_647, width: 1_171, height: 49)
    private let rightArea = NSRect(x: 1_452, y: 1_647, width: 1_172, height: 49)

    func testNotchGeometryDoesNotDependOnVisibleFrame() {
        let menuBarVisible = layout(visibleFrame: NSRect(x: 0, y: 0, width: 2_624, height: 1_646))
        let menuBarHidden = layout(visibleFrame: screenFrame)

        XCTAssertEqual(menuBarVisible.frame, menuBarHidden.frame)
        XCTAssertEqual(menuBarVisible.frame, NSRect(x: 1_011, y: 1_646, width: 601, height: 50))
    }

    func testNotchGeometryUsesOnePhysicalPixelSeamOnRetinaDisplay() {
        let layout = HUDLayout(
            screenFrame: NSRect(x: 0, y: 0, width: 1_000, height: 800),
            visibleFrame: NSRect(x: 0, y: 0, width: 1_000, height: 760),
            backingScaleFactor: 2,
            auxiliaryTopLeftArea: NSRect(x: 0, y: 770, width: 400, height: 30),
            auxiliaryTopRightArea: NSRect(x: 600, y: 770, width: 400, height: 30)
        )

        XCTAssertEqual(layout.frame.minY, 769.5)
        XCTAssertEqual(layout.frame.height, 30.5)
    }

    func testDisplayWithoutNotchUsesVisibleFrameFallback() {
        let layout = HUDLayout(
            screenFrame: NSRect(x: 0, y: 0, width: 1_920, height: 1_080),
            visibleFrame: NSRect(x: 0, y: 0, width: 1_920, height: 1_055),
            backingScaleFactor: 1,
            auxiliaryTopLeftArea: nil,
            auxiliaryTopRightArea: nil
        )

        XCTAssertFalse(layout.hasNotch)
        XCTAssertEqual(layout.frame, NSRect(x: 780, y: 1_005, width: 360, height: 44))
    }

    private func layout(visibleFrame: NSRect) -> HUDLayout {
        HUDLayout(
            screenFrame: screenFrame,
            visibleFrame: visibleFrame,
            backingScaleFactor: 1,
            auxiliaryTopLeftArea: leftArea,
            auxiliaryTopRightArea: rightArea
        )
    }
}
