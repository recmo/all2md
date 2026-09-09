"""Deterministic test backend implementing the production invocation contract."""


class FixtureBackend:
    supports_region_recovery = False

    def recognize_pages(self, images, *, embedded=None):
        results = [self.recognize(image, embedded=None if embedded is None else embedded[i])
                   for i, image in enumerate(images)]
        return "\n<PAGE>\n".join(raw for raw, _ in results), results[0][1]

    def recognize_detail(self, image, *, embedded=None):
        return self.recognize(image, embedded=embedded)

    def recognize_region(self, image):
        raise AssertionError("region recovery is disabled for this fixture")
