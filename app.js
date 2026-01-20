if (process.env.NODE_ENV !== "production" || process.env.NODE_ENV == "docker") {
  require("dotenv").config();
}
const express = require("express");
const expresslayouts = require("express-ejs-layouts");
const methodOverride = require("method-override");
const connectDB = require("./server/config/db");
const session = require("express-session");
const passport = require("passport");
const MongoStore = require("connect-mongo");
const mongoose = require("mongoose");

const required = ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MONGODB_URI"];

for (const key of required) {
  if (!process.env[key]) {
    throw new Error(`Missing required env var: ${key}`);
  }
}
connectDB().then(() => {
  const app = express();
  const port = process.env.PORT || 3000;

  app.use(express.urlencoded({ extended: true }));
  app.use(express.json());
  app.use(methodOverride("_method"));
  app.use(
    session({
      secret: process.env.SESSION_SECRET || "keyboard cat",
      resave: false,
      saveUninitialized: true,
      store: MongoStore.create({
        client: mongoose.connection.getClient(),
        dbName: "notes_app",
      }),
      cookie: { maxAge: 86400000 },
    }),
  );

  app.use(passport.initialize());
  app.use(passport.session());
  // Static files
  app.use(express.static("public"));
  // Set template engine
  app.use(expresslayouts);
  app.set("layout", "./layouts/main");
  app.set("view engine", "ejs");

  // Routes
  app.use("/", require("./server/routes/auth"));
  app.use("/", require("./server/routes/index"));
  app.use("/", require("./server/routes/dashboard"));

  // Handle 404
  app.get("*", function (req, res) {
    res.status(404).render("404");
  });

  app.listen(port, () => {
    if (process.env.NODE_ENV !== "production") {
      console.log(`Server is running on port http://localhost:${port}`);
    }
  });
});
