# engine

Processing engine requests job information from the scitran API, downloads the the specified scitran app and data,
runs the app, and submits the results back to the API.

For more information about what is a scitran-app, see the scitran/api repository on github.

What you will need to get started:
- this repo
    `git clone https://github.com/scitran/engine`
- a SSL key and certificate as a single pem file. Cannot be self-signed.
- a name to identify your processor, you will need this to run the processor, and to register the processor with the scitran-api

`./engine.py https://scitran.example.com/api ./key+cert.pem --log_level debug`

### TODO notes
The engine's ID must be registered with the API server.
TODO: SDM interface should have a way of easily adding a new engine.
- GET /api/engines          - returns list of engines
- PUT /api/engines  + json  - adds the engine to the registered processors list
