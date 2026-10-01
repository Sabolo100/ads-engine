"""Egy javasolt (és esetleg végrehajtott) módosítás: a heti kör, a kreatív-gyár és a havi terv közös adatszerkezete."""
import dataclasses


@dataclasses.dataclass
class Action:
    kind: str                 # add_negative | pause_keyword | rotate_rsa | fix_disapproved | pause_ad | add_keyword | add_images | pause_image | pack_pause_*
    target: str
    reason: str
    status: str = "proposed"  # proposed | applied | validated | rejected | failed
    detail: dict = dataclasses.field(default_factory=dict)
    rejected_because: list = dataclasses.field(default_factory=list)

    def reject(self, *because):
        self.status = "rejected"
        self.rejected_because = list(because)
        return self
