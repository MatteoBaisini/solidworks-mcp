# Projects

The parts you model with the assistant are saved here, one folder per project:

```
projects/
└── bracket/
    ├── cad/       SolidWorks models: bracket_v1_base.SLDPRT, bracket_v2_holes.SLDPRT, ...
    ├── step/      exports written by export_document (one folder per format)
    └── README.md  main dimensions and list of versions
```

To use this folder, point the server at it in your configuration file:

```toml
[projects]
directory = "projects"      # relative to the configuration file, or an absolute path

[export]
directory = "../{format}"   # cad/part.SLDPRT -> step/part.step
```

Everything in this folder except this file is ignored by git, so your parts stay on your PC.
