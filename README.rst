frab-migration
==============

.. image:: https://img.shields.io/pypi/v/pretalx-frab-migration.svg
   :target: https://pypi.org/project/pretalx-frab-migration/
   :alt: PyPI version

This is a plugin for `pretalx`_.
Import conferences from frab into pretalx.

Installation
------------

Install the plugin with pip, in the same environment as your pretalx
installation::

    (env)$ python -m pip install pretalx-frab-migration

Afterwards, run ``migrate`` and ``rebuild`` and restart your pretalx services,
just like after any pretalx update (see `performing updates`_ in the
administrator documentation).

Global settings
---------------

Sign in as an instance administrator. Select an organiser. Select "frab Migration"
in the navigation menu. You can also open
``/orga/organiser/<organiser-slug>/p/pretalx_frab_migration/``.

The page shows the selected organiser. This organiser is the target for
imports. The link is not shown on global pages or event pages.

Enter the base URL of the frab system. Use HTTP or HTTPS. You can leave the field
empty until you configure the connection.

Select the database type and enter the access settings:

* SQLite: Enter the full database file path on the pretalx server. The plugin
  clears the saved host, port, user, and password when you save SQLite settings.
* MySQL or PostgreSQL: Enter the database name. Enter the host and user if the
  database does not use the local defaults. The host can also be a socket path.
  Enter a port, or leave it empty to use the default port (MySQL: 3306;
  PostgreSQL: 5432).

Enter a database password if necessary. The form does not show the saved
password. Leave the field empty to keep it. Enter a new password to replace it.
To remove all database settings, select "Not configured" and save the form.
You can then enter new settings without a password.

Save stores the settings without a connection test.

Connect and select a conference
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Below the settings, select "Connect". The plugin uses the values in the form.
It reads the conference list and saves the settings if the read succeeds.
An empty password field uses the saved password.

After a successful connection, select a conference from the dropdown. Each entry
shows the conference title and acronym. The page shows a message if the database
has no conferences or if the plugin cannot read the list.

The plugin opens the source database for read access. It closes the connection
after it reads the list. Connect again to refresh the list. Conference selection
does not start an import.

SQLite uses Python's database driver. MySQL uses PyMySQL. PostgreSQL uses Psycopg.
The plugin package installs the server database drivers. The database user must
have permission to read the ``conferences`` and ``call_for_participations`` tables.

The plugin stores these settings for the full pretalx installation. Only instance
administrators can read or change them. The plugin does not need activation for
an event.

Migrate the event
~~~~~~~~~~~~~~~~~

Select a conference, then select "Migrate". The plugin creates one event
under the selected organiser. It opens the event settings after creation.
Migrate uses the saved database settings. Connect again if these settings change
or if more than one hour has passed since Connect.

The plugin calls ``pretalx.event.domain.event.create_event``. It passes the name,
short form, timezone, dates, organiser contact email, colour, feedback setting,
schedule visibility setting, and landing page text. It converts the HTML
introduction to Markdown. Links use the configured frab URL as their base.
The default language comes from the pretalx installation.

The event is public if the frab conference has a public schedule. Otherwise it
stays private. This sets event visibility. A public schedule still needs a later
schedule import and release. CfP dates are not imported yet; the initial CfP
uses pretalx defaults and has no deadline.

Rails timezone names are converted to IANA names. For example, ``Berlin`` becomes
``Europe/Berlin``. Timestamps without an offset use UTC, as in the default Rails
configuration. Dates use the conference timezone. A child conference uses its
parent's dates and timezone. Check the source configuration before migration if
it stores timestamps in another timezone.

If the conference email is missing, the plugin uses the contact email from that
conference's CfP. It asks for an email only if both values are missing.

When you select a conference, the plugin checks its short form against all
pretalx events. If it is already in use, the page shows a message and disables
Migrate. The plugin also checks again during creation.

When you select a conference, the form adds any missing required values below
the conference list. Enter the name, short form, timezone, dates, or contact email
if requested. These values apply to the new event. They do not change frab.
If JavaScript is disabled, select "Migrate" to load the required fields first.

pretalx validates the event before it saves it. Invalid data stops creation and
shows an error. Correct form values or source data and try again. An existing
short form also stops creation. Existing events are not updated.

This step imports only the event. The native creation function adds the required
pretalx records, such as the default session type and initial schedule. The plugin
does not yet import frab session types, talks, speakers, rooms, files, or schedules.
The source ``program_export_base_url`` is copied to the HTML Export URL without
conditions. The value is copied as stored. A missing value becomes an empty string.
This also applies to private events. Custom CSS is not imported.

Development setup
-----------------

1. Make sure that you have a working `pretalx development setup`_.

2. Clone this repository, eg to ``local/pretalx-frab-migration``.

3. Activate the virtual environment you use for pretalx development.

4. Run ``pip install -e .`` within this directory to register this application with pretalx's plugin registry.

5. Restart your local pretalx server. Open the global settings page as an instance
   administrator.

Development commands
~~~~~~~~~~~~~~~~~~~~

This plugin uses `just`_ as a task runner and `uv`_ for dependency management.
Run ``just`` with no arguments to list every available command. The most useful ones
are:

``just fmt``
    Auto-format and lint the code.

``just test``
    Run the full test suite with pytest.

Installing pretalx
~~~~~~~~~~~~~~~~~~

The tests need pretalx installed in the environment. ``just test`` handles this for
you: if pretalx cannot be imported, it installs the latest version from git before
running the test suite.

If you already have a development version of pretalx around (for example if you want
to test your changes against a specific commit or branch of pretalx), you can also
install pretalx up front yourself:

``just install-pretalx-local /path/to/pretalx``
    Install pretalx from a local checkout as an editable install.

``just install-pretalx``
    Install the latest pretalx from git (runs before tests if no pretalx is installed).


License
-------

Copyright 2026 Florian Mösch

Released under the terms of the Apache License 2.0


.. _pretalx: https://github.com/pretalx/pretalx
.. _pretalx development setup: https://docs.pretalx.org/en/latest/developer/setup.html
.. _just: https://just.systems/
.. _uv: https://docs.astral.sh/uv/
.. _performing updates: https://docs.pretalx.org/administrator/maintenance/#performing-updates
