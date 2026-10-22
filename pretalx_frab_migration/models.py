from django.db import models


class FrabMigrationSettings(models.Model):
    event = models.OneToOneField(
        to="event.Event",
        on_delete=models.CASCADE,
        related_name="pretalx_frab_migration_settings",
    )
    some_setting = models.CharField(max_length=10, default="A")

    def __str__(self):
        return f"FrabMigrationSettings(event={self.event})"

    @classmethod
    def for_event(cls, event):
        return cls.objects.filter(event=event).first()
