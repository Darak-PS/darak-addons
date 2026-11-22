# Darak Agent

Darak Agent connects this Home Assistant device to Darak for secure remote
management and supported services. It opens no ports on the LAN. Removing the
add-on removes Darak's access to this device.

To enroll, paste the bundle exactly into the add-on's `enrollment_bundle`
configuration field and start the add-on. The add-on clears the bundle after
use.

For troubleshooting, check the add-on logs for `enrollment_bundle_ignored`,
`Could not clear enrollment option`, or `Invalid agent configuration`.

## Support sessions

Choose `allow` to start technician sessions with a Home Assistant notification,
`ask` to approve each request in the Darak support page in the sidebar, or
`block` to prevent support sessions; `block` also refuses remote configuration
changes (write and revert), restarts and entity control. `block` stops
technician sessions and those commands; Darak's cloud still manages household
Home Assistant users, app sign-in, Alexa/MCP tokens, backup restore and updates,
because those features need it. Home Assistant sends a notification when a
session starts and ends, and pushes each notice to every phone with the Home
Assistant app; tapping a pending request opens the Darak support page. Only a
signed-in household member can approve a request; Darak accounts cannot approve
it.
