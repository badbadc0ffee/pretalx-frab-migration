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

The page shows the selected organiser. This organiser is the target for future
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

Saving these settings does not test the connection or import data.

The plugin stores these settings for the full pretalx installation. Only instance
administrators can read or change them. The plugin does not need activation for
an event.

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
